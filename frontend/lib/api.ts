/**
 * 统一的 axios 客户端：
 * - 请求拦截器自动附加 Bearer Token（从 Zustand auth store）
 * - 响应拦截器处理 401：尝试 refresh token，失败则登出
 */
import axios, {
  type AxiosError,
  type AxiosRequestConfig,
  type InternalAxiosRequestConfig,
} from 'axios';

// 浏览器端默认走 Next.js rewrites（同源 /api），避免 CORS
// 服务端或需要跨域直连时可设置 NEXT_PUBLIC_API_BASE_URL
const baseURL = process.env.NEXT_PUBLIC_API_BASE_URL || '';

export const api = axios.create({
  baseURL,
  withCredentials: false,
  headers: {
    'Content-Type': 'application/json',
  },
});

// ── Token 访问（通过闭包函数解耦，避免循环依赖 authStore） ──
type TokenGetter = () => string | null;
type TokensSetter = (access: string, refresh: string) => void;
type AuthClearer = () => void;

let getAccessToken: TokenGetter = () => null;
let getRefreshToken: TokenGetter = () => null;
let setTokens: TokensSetter = () => undefined;
let clearAuth: AuthClearer = () => undefined;

export function bindAuthHandlers(handlers: {
  getAccessToken: TokenGetter;
  getRefreshToken: TokenGetter;
  setTokens: TokensSetter;
  clearAuth: AuthClearer;
}): void {
  getAccessToken = handlers.getAccessToken;
  getRefreshToken = handlers.getRefreshToken;
  setTokens = handlers.setTokens;
  clearAuth = handlers.clearAuth;
}

// ── 请求拦截器 ─────────────────────────────────────────────
api.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = getAccessToken();
  if (token && config.headers) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// ── 401 自动 refresh ──────────────────────────────────────
interface RetriableConfig extends AxiosRequestConfig {
  _retry?: boolean;
}

let refreshPromise: Promise<string> | null = null;

/**
 * 自定义错误：refresh 端点返回 5xx（后端 / DB 池瞬态故障）。
 * 调用方应**不**清 auth，只重试或上报，避免一次后端抖动把用户登出。
 */
class TransientRefreshError extends Error {
  constructor(public httpStatus: number, cause?: unknown) {
    super(`refresh transient ${httpStatus}`);
    this.name = 'TransientRefreshError';
    if (cause !== undefined) (this as { cause?: unknown }).cause = cause;
  }
}

/**
 * 主动刷新 access token（供 SSE 等不经 axios 拦截器的裸连接用）。
 * EventSource 把 token 烧进 URL，过期后原生重连仍带旧 token → 永久 401；
 * 调用方在 onerror 里调本函数刷新 store，token 变化触发组件重连（带新 token）。
 * 复用同一 in-flight refreshPromise，避免并发重复刷新。
 */
export async function refreshAuthToken(): Promise<string> {
  refreshPromise ??= refreshAccessToken();
  try {
    return await refreshPromise;
  } finally {
    refreshPromise = null;
  }
}

async function refreshAccessToken(): Promise<string> {
  const rt = getRefreshToken();
  if (!rt) throw new Error('no refresh token');
  try {
    const resp = await axios.post<{ access_token: string; refresh_token: string }>(
      `${baseURL}/api/auth/refresh`,
      { refresh_token: rt },
    );
    setTokens(resp.data.access_token, resp.data.refresh_token);
    return resp.data.access_token;
  } catch (e) {
    const status = (e as AxiosError)?.response?.status ?? 0;
    // 5xx / 网络错（status=0）= 瞬态：抛 TransientRefreshError，调用方不应清 auth
    if (status === 0 || (status >= 500 && status < 600)) {
      throw new TransientRefreshError(status, e);
    }
    throw e;  // 401 / 403 = refresh token 真无效 → 上层 clearAuth
  }
}

api.interceptors.response.use(
  (r) => r,
  async (error: AxiosError) => {
    const original = error.config as RetriableConfig | undefined;
    if (!original || error.response?.status !== 401 || original._retry) {
      return Promise.reject(error);
    }
    // 登录 / 刷新接口本身的 401 不做重试
    if (original.url?.includes('/api/auth/login') || original.url?.includes('/api/auth/refresh')) {
      return Promise.reject(error);
    }

    original._retry = true;
    try {
      refreshPromise ??= refreshAccessToken();
      const newToken = await refreshPromise;
      refreshPromise = null;
      original.headers = { ...(original.headers ?? {}), Authorization: `Bearer ${newToken}` };
      return api.request(original);
    } catch (refreshErr) {
      refreshPromise = null;
      // v6 fix · 仅在 refresh token 真无效（非 5xx 瞬态）时 clearAuth
      if (!(refreshErr instanceof TransientRefreshError)) {
        clearAuth();
      }
      return Promise.reject(refreshErr);
    }
  },
);

// ── 主动 token 保鲜（供 native fetch / SSE 场景使用）──────
/**
 * 解码 JWT payload（仅 base64 解码，不验签），返回 exp 字段（Unix 秒）；失败返回 null。
 */
function getTokenExp(token: string): number | null {
  try {
    const b64 = token.split('.')[1];
    if (!b64) return null;
    const json = atob(b64.replace(/-/g, '+').replace(/_/g, '/'));
    const payload = JSON.parse(json) as Record<string, unknown>;
    return typeof payload.exp === 'number' ? payload.exp : null;
  } catch {
    return null;
  }
}

/**
 * 确保当前 access token 有效，必要时主动刷新。
 *
 * **为什么需要这个函数：**
 * SSE 流端点（/api/super/{slug}/stream）用 native `EventSource`/`fetch` 而非 axios，
 * 因此 axios 的 401 响应拦截器对其无效。用户若将页面放置超过 30 分钟
 *（ACCESS_TOKEN_EXPIRE_MINUTES），access token 过期后再发消息会直接拿到
 * HTTP 401 并展示"凭证无效"错误，而 refresh token（7天有效）从未被使用。
 *
 * 调用此函数后，若 token 在 60s 内即将过期（或已过期），会提前通过
 * `/api/auth/refresh` 换取新 token 并写入 store，再返回新 token。
 *
 * 多个并发调用共享同一个 refreshPromise，避免同时发多个 refresh 请求。
 *
 * @returns 当前有效的 access token，或在无 token / 刷新失败时返回 null（会清空 auth）。
 */
export async function ensureFreshToken(): Promise<string | null> {
  const token = getAccessToken();
  if (!token) return null;

  const exp = getTokenExp(token);
  const nowSec = Math.floor(Date.now() / 1000);

  // 距过期 ≤ 60s（或无法解析 exp）时主动刷新，避免在 SSE 请求途中 token 刚好过期
  if (exp === null || exp - nowSec <= 60) {
    try {
      refreshPromise ??= refreshAccessToken();
      const newToken = await refreshPromise;
      refreshPromise = null;
      return newToken;
    } catch (e) {
      refreshPromise = null;
      // v6 fix · 5xx 瞬态：保留 token，让调用方拿到 stale token 继续试；
      // 只在 refresh token 真无效时清。
      if (e instanceof TransientRefreshError) {
        return token;  // 用旧 token 重试，比强登出好
      }
      clearAuth();
      return null;
    }
  }

  return token;
}
