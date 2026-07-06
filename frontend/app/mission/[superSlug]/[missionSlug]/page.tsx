'use client';

/**
 * v4 · /super/[slug]/ 3 栏统一 super 工作台
 *
 * 左 240px: thread / 历史会话 + 系统区
 * 中 flex:  当前活动（对话流 + 输入框）— 用户随时跟 super 对话（/btw 风格 cancel）
 * 右 320px: schedules + 最近 worker 调用 + memory / state
 *
 * 与 v3 observe 页相比：
 * - 增加双向对话（实时 cancel + SSE）
 * - 合并 admin/projects/[id] 的 lifecycle 控制
 * - 合并 admin/sessions, admin/memories 内容到右栏 / 子区
 */

import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { useTranslation } from 'react-i18next';
import {
  AlertCircle,
  ChevronLeft,
  Loader2,
  Play,
  RefreshCw,
  RotateCw,
  Square,
  StopCircle,
  Trash2,
  Undo2,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { InputBox, type PendingAttachment } from '@/components/chat/InputBox';
import { ApprovalCard, type ApprovalCardData } from '@/components/chat/ApprovalCard';
import { approvalsApi } from '@/lib/api/approvals';
import { FormRequestCard } from '@/components/chat/FormRequestCard';
import { ArtifactPreview } from '@/components/chat/ArtifactPreview';
import { MemoryTab } from '@/components/super/MemoryTab';
import { ScheduleEditor } from '@/components/super/ScheduleEditor';
import { AutoApproveToggle } from '@/components/super/AutoApproveToggle';
// V7.4 · ChatTickCard / useActivityStream / ActivityTree 已退役（ADR-007）
// ADR-008 P1 · 消息驱动的 tick 折叠卡（修 V7.4 daemon agent_log 刷屏）
import { MessageTickCard } from '@/components/mission/MessageTickCard';
import { systemUserKind } from '@/lib/chat/systemMessage';
import { BuilderWorkLogPanel } from '@/components/mission/BuilderWorkLogPanel';
import { RedirectSuggestionCard, type RedirectSuggestionData } from '@/components/mission/RedirectSuggestionCard';
import { observeV3Api, type SuperStats, type SuperThread, type SuperThreadsResp } from '@/lib/api/observeV3';
import type { ChatMessage, LiveCall, SSEEvent } from '@/types/sse';
import type { MessageLike } from '@/lib/chat/timeline';
import type { MissionLifecycleAction } from '@/types/mission';
import { errMessage } from '@/lib/errors';

type RightTab = 'activity' | 'schedule' | 'memory' | 'threads';
import { refreshAuthToken } from '@/lib/api';
import {
  superConversationApi,
  type ChatAttachment,
} from '@/lib/api/superConversation';
import { missionsAdminApi } from '@/lib/api/missionsAdmin';
import { missionsApi, type MissionPublic } from '@/lib/api/missions';
import Link from 'next/link';
import { Dialog } from '@/components/ui/dialog';
import { dispatchSSEEvent } from '@/lib/sse/handlers';
import { assembleMissionTimeline } from '@/lib/chat/missionTimeline';
import { storageApi } from '@/lib/api/storage';
import { randomUUID } from '@/lib/utils';
import { useAuthStore } from '@/stores/authStore';
import { useConfirm } from '@/components/providers/ConfirmProvider';

type Message = ChatMessage;

// FIX A · 把任意含 `/colony/<...>` 的字符串（带 host+query 的完整 URL，或裸 `colony/...` key）
// 归一成从 `colony/` 起的规范 key（去掉 host 与 `?query`）。无 `colony/` 段返回 null。
// e.g. http://localhost:19000/colony/aux-image/x.jpg?X-Amz-... → colony/aux-image/x.jpg
function s3KeyFromUrl(u: string): string | null {
  if (!u) return null;
  const idx = u.indexOf('colony/');
  if (idx === -1) return null;
  let key = u.slice(idx);
  const q = key.indexOf('?');
  if (q !== -1) key = key.slice(0, q);
  const hash = key.indexOf('#');
  if (hash !== -1) key = key.slice(0, hash);
  return key || null;
}

// FIX A · 后端公开代理：流式回任意 `colony/...` S3 对象（正确 content-type + CORS，无鉴权/无过期）。
// 相对路径——Next rewrites 已把 /api/* 转发到后端（next.config.js）。
function proxyUrl(key: string): string {
  return `/api/storage/proxy?key=${encodeURIComponent(key)}`;
}

// FIX A · 从消息正文里抓 S3 artifact URL（完整 URL 或裸 key），按 key 去重返回。
const ARTIFACT_URL_RE =
  /(?:https?:\/\/[^\s)"'\]]*)?colony\/(?:aux-image|workspace|deliverables)\/[^\s)"'\]]+/g;
function extractArtifactKeys(content: string): string[] {
  if (!content) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  const re = new RegExp(ARTIFACT_URL_RE.source, 'g');
  let m: RegExpExecArray | null;
  while ((m = re.exec(content)) !== null) {
    const key = s3KeyFromUrl(m[0]);
    if (key && !seen.has(key)) {
      seen.add(key);
      out.push(key);
    }
  }
  return out;
}

export default function SuperWorkstation() {
  const { t } = useTranslation();
  const confirm = useConfirm();
  const params = useParams<{ superSlug: string; missionSlug: string }>();
  const router = useRouter();
  const slug = params.missionSlug; // 数据按 mission slug 加载
  const superSlug = params.superSlug; // URL 里的 super 段（建链接用）
  const token = useAuthStore((s) => s.accessToken);
  const [threads, setThreads] = useState<SuperThread[]>([]);
  const [activeThreadKey, setActiveThreadKey] = useState<string | null>(null);  // ADR-018 mission-only
  // Missions (instances) of this super — the left rail lists them; switch by navigating.
  const [missions, setMissions] = useState<MissionPublic[]>([]);
  const [newMissionOpen, setNewMissionOpen] = useState(false);
  const autoOpenedNewMissionRef = useRef(false);  // 空 builder mission 自动弹新建对话框（一次性）
  const [newMissionName, setNewMissionName] = useState('');
  const [newMissionHint, setNewMissionHint] = useState('');
  const [spawning, setSpawning] = useState(false);
  const [stats, setStats] = useState<SuperStats | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [project, setProject] = useState<SuperThreadsResp | null>(null);
  const [input, setInput] = useState('');
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [sending, setSending] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<{
    lifecycle_status?: string;
    is_running?: boolean;
    pending_count?: number;
  }>({});
  const [err, setErr] = useState<string | null>(null);
  const [lifecycleBusy, setLifecycleBusy] = useState<string | null>(null);
  // v5 · 实时 worker 调用流（最近 30 条）
  const [liveCalls, setLiveCalls] = useState<LiveCall[]>([]);
  // v5 · 内联审批卡片（按 request_id 聚合；resolved 时合并 resolution）
  const [approvals, setApprovals] = useState<ApprovalCardData[]>([]);
  // ADR-010 UI · request_structured_input 表单：已提交的 request_id 集合（提交后置灰）
  const [submittedForms, setSubmittedForms] = useState<Record<string, boolean>>({});
  // V7.4 · ActivityTree 退役；daemon 细节走 chat 消息流
  // v6.J.4 · redirect 建议卡列表
  const [redirects, setRedirects] = useState<RedirectSuggestionData[]>([]);
  // v5 · 右栏 tab 切换：activity / schedule / memory
  const [rightTab, setRightTab] = useState<RightTab>('activity');
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const esRef = useRef<EventSource | null>(null);
  // ADR-030 · 用户刚操作(发消息/点审批)后到 AI 首个输出前的乐观「等待回复」态：
  // 立即在 AI 回复位置显示 loading 气泡，避免用户误以为程序卡死。SSE 的 is_running 投递偶有
  // 延迟，故不单靠它——本地 flag 一操作即亮，首个新 AI 输出到达/token 直播/超时即灭。
  const [awaitingReply, setAwaitingReply] = useState(false);
  const awaitingBaselineRef = useRef(0);
  const awaitingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const beginAwaitingReply = () => {
    awaitingBaselineRef.current = messages.filter(
      (m) => m.role !== 'user' && m.id !== 'stream-live',
    ).length;
    setAwaitingReply(true);
    if (awaitingTimerRef.current) clearTimeout(awaitingTimerRef.current);
    // 安全兜底：120s 无输出自动灭，避免永久转圈
    awaitingTimerRef.current = setTimeout(() => setAwaitingReply(false), 120_000);
  };

  // ADR-018 mission-only · 切换查看哪个 thread 的消息流（thread_key 标识）
  const switchToThread = async (threadKey: string) => {
    setActiveThreadKey(threadKey);
    try {
      const raw = await observeV3Api.exportThread(slug, threadKey, 'json').catch(() => null);
      if (raw && typeof raw === 'object' && 'messages' in raw) {
        setMessages((raw as { messages: Message[] }).messages || []);
      } else {
        setMessages([]);
      }
    } catch (e) {
      setErr(errMessage(e));
    }
  };

  const refreshAll = async () => {
    try {
      const [thr, st] = await Promise.all([
        observeV3Api.superThreads(slug),
        observeV3Api.superStats(slug, '7d').catch(() => null),
      ]);
      setThreads(thr.threads);
      setStats(st);
      setProject(thr);
      // ADR-025 follow-up · REST 兜底拉 pending 审批：SSE init 帧经代理/EventSource 偶发投递不到
      // → 仅靠 SSE 时刷新后 approvals 为空、pending 卡误渲染成「已关闭」不可点。REST 拉一次保证
      // 刷新后 pending 卡可点（与 SSE 去重合并，已决卡仍由 message 重建保持禁用）。
      if (thr.mission_id) {
        approvalsApi.listForProject(thr.mission_id, true)
          .then((rows) => setApprovals((prev) => {
            const seen = new Set(prev.map((a) => a.request_id));
            const fresh = rows.filter((r) => !seen.has(r.request_id)).map((r) => ({
              request_id: r.request_id,
              title: r.title,
              message: r.message,
              options: r.options,
              created_at: r.created_at,
              thread_key: r.thread_key ?? undefined,
              status: r.status,
            } as ApprovalCardData));
            return fresh.length ? [...prev, ...fresh] : prev;
          }))
          .catch(() => {});
      }
      // Load this super's missions for the left rail. 系统 mission（Builder / Worker-Opt 等）的
      // 接线照常工作，但**不进用户可见列表**；用户 mission 列表为空 → 自动弹「新建 Mission」引导
      // （一次性 ref 守卫）。这样全新安装进工作台就是空台 + 引导，而非预置 demo。
      if (thr.supervisor_agent_id) {
        missionsApi.list(thr.supervisor_agent_id)
          .then((list) => {
            // FIX D · 展示所有 mission（含系统 mission，如 Worker-Optimization 的唯一 is_system mission）。
            setMissions(list);
            // 仅当完整列表为空时才自动弹「新建 Mission」引导（Builder Supervisor 0 mission 仍正确弹出）。
            if (list.length === 0 && !autoOpenedNewMissionRef.current) {
              autoOpenedNewMissionRef.current = true;
              setNewMissionOpen(true);
            }
          })
          .catch(() => {});
      }
      // 默认选当前 activeThreadKey；首次选主线 'main'；否则第 1 个 thread
      const main = thr.threads.find((t) => t.thread_kind === 'super_main_runtime') || thr.threads[0];
      const wantKey = activeThreadKey
        && thr.threads.some((t) => t.thread_key === activeThreadKey)
        ? activeThreadKey
        : main?.thread_key;
      if (wantKey) {
        if (wantKey !== activeThreadKey) setActiveThreadKey(wantKey);
        const raw = await observeV3Api.exportThread(slug, wantKey, 'json').catch(() => null);
        if (raw && typeof raw === 'object' && 'messages' in raw) {
          const msgs = (raw as { messages: Message[] }).messages || [];
          setMessages(msgs);
          // 「空列表自动弹新建」已统一到上面的 missions 加载逻辑（按 is_system 过滤后判空），
          // 这里不再单独按 slug==='builder' 触发。
        }
      }
    } catch (e) {
      setErr(errMessage(e));
    }
  };

  useEffect(() => {
    void refreshAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  // ADR-030 · 首个新 AI 输出（agent_log/assistant/tick 或 token 直播）到达即熄灭「等待回复」气泡。
  useEffect(() => {
    if (!awaitingReply) return;
    const hasLiveToken = messages.some((m) => m.id === 'stream-live');
    const nonUser = messages.filter((m) => m.role !== 'user' && m.id !== 'stream-live').length;
    if (hasLiveToken || nonUser > awaitingBaselineRef.current) {
      setAwaitingReply(false);
      if (awaitingTimerRef.current) clearTimeout(awaitingTimerRef.current);
    }
  }, [messages, awaitingReply]);

  // Spawn a new mission (instance) of this super, then switch to it.
  async function spawnMission() {
    if (!project?.supervisor_agent_id || !newMissionName.trim() || spawning) return;
    setSpawning(true);
    setErr(null);
    try {
      const res = await missionsApi.create({
        super_agent_id: project.supervisor_agent_id,
        name: newMissionName.trim(),
        goal_hint: newMissionHint.trim() || undefined,
      });
      if (res.ok && res.mission) {
        setNewMissionOpen(false);
        setNewMissionName('');
        setNewMissionHint('');
        router.push(`/mission/${res.mission.super_slug || superSlug}/${res.mission.slug}`);
      } else {
        setErr(res.error || t('missionCards.redirectFailed'));
      }
    } catch (e) {
      setErr(errMessage(e));
    } finally {
      setSpawning(false);
    }
  }

  // SSE: 实时拉 super 状态 + 新消息
  useEffect(() => {
    if (!token || !slug) return;
    const url = superConversationApi.streamUrl(slug, token);
    const es = new EventSource(url, { withCredentials: false });
    esRef.current = es;
    es.onmessage = (ev) => {
      try {
        const data: SSEEvent = JSON.parse(ev.data);
        // R2-6 · 单一 typed dispatch（lib/sse/handlers.ts）
        dispatchSSEEvent(data, {
          setStreamState,
          setApprovals,
          setMessages,
          setRedirects,
          setLiveCalls,
          handleActivityEvent: () => {},  // V7.4 · ActivityTree 退役，activity 事件忽略
        });
      } catch {
        /* ignore */
      }
    };
    let refreshedOnce = false;
    es.onerror = () => {
      // EventSource 把 token 烧进 URL，过期后原生重连仍带旧 token → 永久 401（会话卡片
      // 不再实时刷新、审批状态僵死）。主动刷新一次 token：store 更新 → 本 effect 依赖 token
      // 变化 → 重建 EventSource 带新 token。刷新失败（refresh token 也过期）时 axios 层会
      // 清 auth 跳登录，这里不重复处理。guard 防紧循环刷新。
      if (refreshedOnce) return;
      refreshedOnce = true;
      refreshAuthToken().catch(() => {
        /* refresh 失败：axios 拦截器负责 clearAuth；此处静默 */
      });
    };
    return () => {
      es.close();
      esRef.current = null;
    };
  }, [slug, token]);

  // 自动滚动到底部：新消息 / 新审批卡 / 切线程 / 加载刷新 都滚到底。
  // 用 'auto'（瞬时）而非 'smooth'——刷新时大量消息首次布局未完成，smooth 常滚不到底；
  // rAF 二次兜底：等图片/卡片布局高度稳定后再滚一次，确保稳定贴底。
  useEffect(() => {
    const el = messagesEndRef.current;
    if (!el) return;
    el.scrollIntoView({ behavior: 'auto' });
    const id = requestAnimationFrame(() => el.scrollIntoView({ behavior: 'auto' }));
    return () => cancelAnimationFrame(id);
  }, [messages.length, approvals.length, activeThreadKey]);

  async function addFiles(files: FileList | File[]) {
    const list = Array.from(files);
    for (const f of list) {
      const id = randomUUID();
      const isImage = f.type.startsWith('image/');
      const draft: PendingAttachment = {
        id,
        kind: isImage ? 'image' : 'file',
        name: f.name,
        size: f.size,
        url: null,
        key: null,
        mediaType: f.type || 'application/octet-stream',
        uploading: true,
      };
      setAttachments((prev) => [...prev, draft]);
      try {
        const resp = await storageApi.userUpload(f);
        setAttachments((prev) =>
          prev.map((a) =>
            a.id === id
              ? { ...a, uploading: false, url: resp.url, key: resp.key }
              : a,
          ),
        );
      } catch (e) {
        setAttachments((prev) =>
          prev.map((a) =>
            a.id === id
              ? { ...a, uploading: false, error: e instanceof Error ? e.message : t('workbench.uploadFailed') }
              : a,
          ),
        );
      }
    }
  }
  function removeAttachment(id: string) {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }

  const send = async () => {
    const content = input.trim();
    const hasAtts = attachments.length > 0;
    // 运行中禁止发送（与 InputBox disabled 一致的兜底）：避免 cancel+重跑导致的抖动/500。
    if ((!content && !hasAtts) || sending || streamState.is_running) return;
    if (attachments.some((a) => a.uploading)) {
      setFeedback(t('workbench.attachmentsUploading'));
      return;
    }
    setSending(true);
    setErr(null);
    setFeedback(null);
    try {
      const chatAtts: ChatAttachment[] = attachments
        .filter((a) => a.url)
        .map((a) => ({
          kind: a.kind,
          name: a.name,
          url: a.url as string,
          mediaType: a.mediaType,
          size: a.size,
        }));
      const res = await superConversationApi.chat(slug, {
        content,
        attachments: chatAtts.length > 0 ? chatAtts : undefined,
      });
      if (!res.ok) {
        setErr(res.error || 'send failed');
      } else {
        setInput('');
        setAttachments([]);
        // 立即追加到本地（SSE 也会推一遍，dedup by id）
        const displayContent =
          content + (chatAtts.length > 0 ? `\n\n${chatAtts.map((a) => (a.kind === 'image' ? `🖼️ ${a.name}` : `📎 ${a.name}`)).join('\n')}` : '');
        const tmp: Message = {
          id: res.message_id || `tmp-${Date.now()}`,
          role: 'user',
          content: displayContent,
          meta: { _local: true, attachments: chatAtts },
          created_at: new Date().toISOString(),
        };
        // dedup：SSE 可能在 await chat() 返回前已把同 id 的真实消息推进来 → 不要再叠一条
        setMessages((prev) => (prev.some((x) => x.id === tmp.id) ? prev : [...prev, tmp]));
        beginAwaitingReply();  // ADR-030 · 发消息后立即在 AI 回复位置显示 loading 气泡
        // 反馈：组合 auto_started / triggered / warning 给用户清晰信息
        const parts: string[] = [];
        if (res.auto_started) parts.push(`🟢 ${t('workbench.superAutoStarted')}`);
        if (res.triggered_tick) parts.push(`⚡ ${t('workbench.tickTriggered')}`);
        if (res.cancel_result && res.cancel_result.stage)
          parts.push(`🛑 ${t('workbench.tickCancelled', { stage: res.cancel_result.stage as string })}`);
        if (res.warning) parts.push(`⚠️ ${res.warning}`);
        if (!res.triggered_tick && !res.auto_started && !res.warning) {
          parts.push(
            t('workbench.messageQueued', {
              lifecycle: res.lifecycle_after || '?',
              count: res.queue_size_after,
            }),
          );
        }
        setFeedback(parts.join(' · '));
        setTimeout(() => setFeedback(null), 6000);
      }
    } catch (e) {
      setErr(errMessage(e));
    } finally {
      setSending(false);
    }
  };

  // ADR-010 UI · 提交 request_structured_input 表单 → 走 chat 通道发 [form_response …]
  const submitForm = async (requestId: string, values: Record<string, unknown>) => {
    const payload = `[form_response request_id=${requestId}]\n\n${JSON.stringify(values, null, 2)}`;
    try {
      const res = await superConversationApi.chat(slug, { content: payload });
      if (!res.ok) {
        setErr(res.error || t('workbench.formSubmitFailed'));
        return;
      }
      setSubmittedForms((prev) => ({ ...prev, [requestId]: true }));
      setFeedback(`✅ ${t('workbench.formSubmitted')}`);
      setTimeout(() => setFeedback(null), 6000);
    } catch (e) {
      setErr(errMessage(e));
    }
  };

  const interrupt = async () => {
    try {
      await superConversationApi.interrupt(slug);
    } catch (e) {
      setErr(errMessage(e));
    }
  };

  // v4.3 · 删除单条消息
  const deleteMessage = async (m: Message) => {
    if (!(await confirm({ message: t('workbench.deleteMessageConfirm', { role: m.role, preview: m.content.slice(0, 100) }), danger: true }))) return;
    try {
      const res = await superConversationApi.deleteMessage(slug, m.id);
      // 立即从 UI 移除
      setMessages((prev) => prev.filter((x) => x.id !== m.id));
      const parts = [`🗑️ ${t('workbench.deletedOne')}`];
      if (res.dropped_pending > 0) parts.push(t('workbench.queueDropped', { count: res.dropped_pending }));
      setFeedback(parts.join(' · '));
      setTimeout(() => setFeedback(null), 4000);
    } catch (e) {
      setErr(errMessage(e));
    }
  };

  // v4.3 · rewind 到这条消息（删除其后所有消息）
  const rewindTo = async (m: Message) => {
    const ok = await confirm({ message: t('workbench.rewindConfirm'), danger: true });
    if (!ok) return;
    try {
      const res = await superConversationApi.rewindTo(slug, m.id, true);
      // 立即剪掉 UI 中 created_at > target 的消息
      const target = m.created_at;
      setMessages((prev) =>
        prev.filter((x) => !target || !x.created_at || x.created_at <= target),
      );
      const parts = [`↩ ${t('workbench.rewindDeleted', { count: res.deleted_messages })}`];
      if (res.dropped_pending > 0) parts.push(t('workbench.queueDropped', { count: res.dropped_pending }));
      if (res.cancelled_current_tick) parts.push(t('workbench.cancelledCurrentTick'));
      setFeedback(parts.join(' · '));
      setTimeout(() => setFeedback(null), 5000);
    } catch (e) {
      setErr(errMessage(e));
    }
  };

  const lifecycle = async (action: MissionLifecycleAction) => {
    if (!project?.mission_id) return;  // 无 standing mission 的空壳工作台：没有 mission 可启停
    setLifecycleBusy(action);
    setErr(null);
    try {
      await missionsAdminApi.lifecycle(project.mission_id, action);
      await refreshAll();
    } catch (e) {
      setErr(errMessage(e));
    } finally {
      setLifecycleBusy(null);
    }
  };

  const lifecycleBadge = useMemo(() => {
    // 有未决审批 → 语义上「等待人类」，即使 lifecycle 仍是 running（daemon 不空转、批后自动续）。
    const hasPendingApproval = approvals.some((a) => !a.resolution);
    if (hasPendingApproval) {
      return (
        <span className="px-2 py-0.5 rounded text-xs bg-warning/10 text-warning">⏳ {t('workbench.waitingApproval')}</span>
      );
    }
    const s = streamState.lifecycle_status || project?.lifecycle_status || 'unknown';
    const color =
      s === 'running'
        ? 'bg-success/10 text-success'
        : s === 'paused_waiting_capability'
          ? 'bg-warning/10 text-warning'
          : s === 'error'
            ? 'bg-destructive/10 text-destructive'
            : 'bg-muted text-muted-foreground';
    return <span className={`px-2 py-0.5 rounded text-xs ${color}`}>{s}</span>;
  }, [streamState, project, approvals, t]);

  return (
    <div className="h-screen flex flex-col">
      <header className="border-b border-border p-3 pr-32 flex items-center gap-3 bg-card">
        <Button variant="ghost" size="sm" onClick={() => router.push('/admin/agents?tab=super')}>
          <ChevronLeft className="w-4 h-4" />
        </Button>
        <h1 className="font-semibold">Super · {project?.super_name || project?.name || slug}</h1>
        {project?.name && (
          <span className="text-xs text-muted-foreground truncate max-w-[200px]">/ {project.name}</span>
        )}
        {lifecycleBadge}
        {streamState.is_running
          && (streamState.pending_count ?? 0) === 0
          && !['paused_clarification', 'paused_waiting_capability', 'paused_idle', 'stopped'].includes(
            streamState.lifecycle_status || project?.lifecycle_status || '',
          ) && (
          <span className="text-xs text-primary flex items-center gap-1">
            <Loader2 className="w-3 h-3 animate-spin" /> {t('workbench.ticking')}
          </span>
        )}
        {(streamState.pending_count ?? 0) > 0 && (
          <span className="text-xs text-warning">{t('workbench.pendingMessages', { count: streamState.pending_count })}</span>
        )}
        {project?.paused_reason && (
          <span className="text-xs text-warning">{project.paused_reason}</span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {project?.mission_id && (
            <AutoApproveToggle
              projectId={project.mission_id}
              forcedAuto={
                threads.find((th) => th.thread_key === activeThreadKey)?.thread_kind === 'worker_health'
              }
            />
          )}
          <Button
            size="sm"
            variant="outline"
            disabled={lifecycleBusy !== null || streamState.is_running}
            onClick={() => lifecycle('run_once')}
          >
            <Play className="w-3.5 h-3.5 mr-1" /> {t('workbench.runOnce')}
          </Button>
          <Button
            size="sm"
            disabled={lifecycleBusy !== null || streamState.is_running}
            onClick={() => lifecycle('start')}
          >
            {t('workbench.start')}
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={lifecycleBusy !== null}
            onClick={() => lifecycle('stop')}
          >
            <Square className="w-3.5 h-3.5 mr-1" /> {t('workbench.stop')}
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={lifecycleBusy !== null || streamState.is_running}
            onClick={() => lifecycle('restart')}
          >
            <RotateCw className="w-3.5 h-3.5 mr-1" /> {t('workbench.restart')}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => refreshAll()}>
            <RefreshCw className="w-3.5 h-3.5" />
          </Button>
        </div>
      </header>

      {err && (
        <div className="px-4 py-2 bg-destructive/10 text-destructive text-sm flex items-center gap-2">
          <AlertCircle className="w-4 h-4" />
          {err}
        </div>
      )}

      <div className="flex-1 flex min-h-0">
        {/* 左栏 · Missions(实例)列表 + 本 mission 的会话 */}
        <aside className="w-60 border-r border-border overflow-y-auto bg-muted/30">
          {/* Missions of this super — switch by navigating, "+ New" spawns an instance. */}
          <div className="p-2 text-xs text-muted-foreground border-b border-border flex items-center justify-between bg-muted/40">
            <span className="font-medium">{t('workbench.missions', { count: missions.length })}</span>
            <button
              className="text-[10px] text-primary hover:underline"
              title={t('workbench.newMissionHint')}
              onClick={() => setNewMissionOpen(true)}
            >
              + {t('workbench.newMission')}
            </button>
          </div>
          {missions.map((m) => {
            const isCurrent = m.slug === slug;
            return (
              <div
                key={m.id}
                className={`group relative border-b border-border ${
                  isCurrent ? 'bg-accent border-l-2 border-l-primary' : 'hover:bg-muted/50'
                }`}
              >
                <Link href={`/mission/${superSlug}/${m.slug}`} onClick={() => { if (isCurrent) switchToThread('main'); }} className="block px-3 py-2 text-xs">
                  <div className={`truncate pr-5 text-foreground ${isCurrent ? 'font-medium' : ''}`}>{m.name}</div>
                  <div className="text-[10px] text-muted-foreground mt-0.5">{m.lifecycle_status}</div>
                </Link>
                {/* 删除 mission（消息/工作区/记忆一并删；后端拦系统对象如 Builder 返回 409）。super 保留（可能被共享）。 */}
                {/* FIX D · 系统 mission（is_system）不可删：隐藏删除控件。 */}
                {!m.is_system && (
                <button
                  title={t('workbench.deleteMissionHint')}
                  className="absolute top-1 right-1 opacity-0 group-hover:opacity-100 p-0.5 text-destructive hover:bg-destructive/10 rounded"
                  onClick={async (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    if (!(await confirm({ message: t('workbench.deleteMissionConfirm', { name: m.name }), danger: true }))) return;
                    try {
                      await missionsAdminApi.delete(m.id);
                      if (m.slug === slug) {
                        router.push('/super/builder');
                      } else {
                        setMissions((prev) => prev.filter((x) => x.id !== m.id));
                      }
                    } catch (err) {
                      setErr(errMessage(err));
                    }
                  }}
                >
                  <Trash2 className="w-3 h-3" />
                </button>
                )}
              </div>
            );
          })}

          {/* ADR-024 S3 · 线程列表移到右侧「线程」tab；清记忆移到「记忆」tab；左栏只保留 Missions 实例列表 */}
        </aside>

        {/* 中栏 对话流 */}
        <main className="flex-1 flex flex-col min-w-0">
          {/* ADR-024 #11 · 当前线程面包屑（点左栏 Mission 回主线） */}
          <div className="px-4 py-1.5 border-b border-border text-[11px] text-muted-foreground bg-muted/20 flex items-center gap-1.5">
            {(() => {
              const th = threads.find((x) => x.thread_key === activeThreadKey);
              if (!th || th.thread_kind === 'super_main_runtime') return <span>🧵 {t('workbench.mainLine')}</span>;
              return <span>🧵 {th.title?.trim() || th.thread_key} · <span className="italic">{t('workbench.readOnlyTag')}</span></span>;
            })()}
          </div>
          <div className="flex-1 overflow-y-auto p-4 space-y-2">
            {/* V7.4 · ActivityTree/ChatTickCard 已退役（ADR-007）；daemon 细节现以消息形式进 chat 时间线 */}
            {/* v6.J.4 · Q7 mismatch redirect 卡 */}
            {redirects.map((r, i) => (
              <RedirectSuggestionCard
                key={i}
                data={r}
                onResolved={() => {
                  // 按对象引用过滤（修：原按 index 过滤，移除后索引漂移会删错卡）
                  setRedirects((prev) => prev.filter((x) => x !== r));
                }}
              />
            ))}
            {/* v6 fix · approvals + messages 按 created_at 时间序混编渲染
                ADR-008 P1 · 同 turn_id 的 daemon tick 消息折叠成一张 MessageTickCard（修 V7.4 刷屏） */}
            {(() => {
              // R · 时间线装配抽到 lib/chat/missionTimeline.ts（纯函数，vitest 覆盖）
              // ADR-024 #3 · 审批卡按当前线程过滤（审批属 main；worker 线程不再串显 main 审批）
              const _curThread = activeThreadKey || 'main';
              const _visibleApprovals = approvals.filter(
                (a) => (a.thread_key || 'main') === _curThread,
              );
              const items = assembleMissionTimeline(messages, _visibleApprovals);
              return items.map((it) => {
                if (it.kind === 'tick') {
                  return <MessageTickCard key={`tick-${it.turnId}`} messages={it.data as MessageLike[]} />;
                }
                if (it.kind === 'approval') {
                  // 重建的审批卡运行时带 title/message/options（missionTimeline 装配时填入），
                  // 但 TimelineApproval 只声明 request_id —— 收窄回 ApprovalCardData。
                  const a = it.data as ApprovalCardData;
                  return (
                    <ApprovalCard
                      key={`approval-${a.request_id}`}
                      data={a}
                      onResolved={(option) => {
                        setApprovals((prev) =>
                          prev.map((x) =>
                            x.request_id === a.request_id
                              ? { ...x, resolution: { option, decided_by: 'inline-card', via: 'ui' } }
                              : x,
                          ),
                        );
                        beginAwaitingReply();  // ADR-030 · 点审批后立即显示 loading 气泡（决卡→续跑 tick 有空窗）
                      }}
                    />
                  );
                }
                if (it.kind === 'cta') {
                  const cm = it.data;
                  const pslug = String(cm.meta?.project_slug);
                  const pname = cm.meta?.project_name || pslug;
                  return (
                    <div key={`cta-${cm.id}`} className="mx-auto max-w-[85%] rounded-[14px] border border-success/40 bg-success/10 p-4 text-sm">
                      <div className="font-semibold text-success mb-2">✅ {t('workbench.missionBuiltActivated', { name: pname })}</div>
                      <p className="text-[12px] text-success mb-3">{t('workbench.missionBuiltHint')}</p>
                      <Button
                        size="sm"
                        onClick={() => window.open(`/mission/${pslug}`, '_blank')}
                      >
                        {t('workbench.enterSuper')} →
                      </Button>
                    </div>
                  );
                }
                if (it.kind === 'form') {
                  const fm = it.data;
                  const rid = String(fm.meta?.request_id);
                  return (
                    <FormRequestCard
                      key={`form-${rid}`}
                      item={{
                        id: rid,
                        title: fm.meta?.title,
                        description: fm.meta?.description,
                        schema: fm.meta?.schema || {},
                        prefilled: fm.meta?.prefilled,
                        submitLabel: fm.meta?.submit_label,
                        state: submittedForms[rid] ? 'submitted' : 'pending',
                      }}
                      onSubmit={submitForm}
                    />
                  );
                }
                const m = it.data;
                const isLocalTmp = m.id.startsWith('tmp-') || m.meta?._local || m.meta?._streaming;
                // 系统用 user 角色喂 LLM 的消息（派单/健康自检/降级/升级/审批回复）——
                // 不显示成「真人蓝色右对齐气泡」，改中性样式 + 「🤖 系统·来源」标识，区别真人。
                const sysKind = systemUserKind(m.role, m.meta);
                const isHumanUser = m.role === 'user' && !sysKind;
                return (
                  <div
                    key={`msg-${m.id}`}
                    className={`group relative max-w-3xl rounded p-2 text-sm ${
                      isHumanUser
                        ? 'bg-primary/10 ml-auto'
                        : sysKind
                          ? 'bg-muted/40 border border-dashed border-border text-muted-foreground'
                          : m.role === 'assistant'
                            ? 'bg-card border border-border'
                            : 'bg-muted text-muted-foreground text-xs'
                    }`}
                  >
                    <div className="text-[10px] text-muted-foreground mb-0.5 flex items-center gap-2">
                      <span>
                        {sysKind
                          ? `🤖 ${t('workbench.systemMsg')} · ${t(`workbench.sysMsgKind.${sysKind}`)}`
                          : m.role}{' '}
                        · {m.created_at?.slice(11, 19) || ''}
                      </span>
                      {!isLocalTmp && (
                        <span className="ml-auto opacity-0 group-hover:opacity-100 transition flex gap-1">
                          <button
                            title={t('workbench.rewindHint')}
                            className="text-warning hover:bg-warning/10 rounded px-1"
                            onClick={() => void rewindTo(m)}
                          >
                            <Undo2 className="w-3 h-3" />
                          </button>
                          <button
                            title={t('workbench.deleteMessageHint')}
                            className="text-destructive hover:bg-destructive/10 rounded px-1"
                            onClick={() => void deleteMessage(m)}
                          >
                            <Trash2 className="w-3 h-3" />
                          </button>
                        </span>
                      )}
                    </div>
                    <pre className="whitespace-pre-wrap break-words text-xs">
                      {(m.content || '').slice(0, 4000)}
                      {m.meta?._streaming && <span className="animate-pulse">▍</span>}
                    </pre>
                    {(() => {
                      // FIX A · 收集本条消息已通过 meta.artifact_url 展示的 key，避免与正文扫描重复渲染。
                      const shownKeys = new Set<string>();
                      const blocks: ReactNode[] = [];
                      if (m.meta?.artifact_url) {
                        // 把可能过期的 presigned URL 换成永不过期的代理 URL（提不出 key 时回退原 URL）。
                        const key = s3KeyFromUrl(m.meta.artifact_url);
                        if (key) shownKeys.add(key);
                        blocks.push(
                          <ArtifactPreview
                            key="meta-artifact"
                            artifact={{
                              url: key ? proxyUrl(key) : m.meta.artifact_url,
                              name: m.meta.artifact_meta?.label || m.meta.action,
                              mediaType: m.meta.media_type,
                              size: m.meta.artifact_bytes,
                            }}
                          />,
                        );
                      }
                      if (Array.isArray(m.meta?.attachments)) {
                        (m.meta.attachments as Array<{
                          url: string; name?: string; mediaType?: string; size?: number;
                        }>).forEach((a, i) => {
                          const key = s3KeyFromUrl(a.url);
                          if (key) shownKeys.add(key);
                          blocks.push(
                            <ArtifactPreview
                              key={`att-${i}`}
                              artifact={{
                                url: key ? proxyUrl(key) : a.url,
                                name: a.name,
                                mediaType: a.mediaType,
                                size: a.size,
                              }}
                            />,
                          );
                        });
                      }
                      // FIX A · 扫描正文里 LLM 贴的 S3 URL（aux-image 等），按 key 去重后内联预览+下载。
                      if (m.role !== 'user' || sysKind) {
                        for (const key of extractArtifactKeys(m.content || '')) {
                          if (shownKeys.has(key)) continue;
                          shownKeys.add(key);
                          blocks.push(
                            <ArtifactPreview key={`text-${key}`} artifact={{ url: proxyUrl(key) }} />,
                          );
                        }
                      }
                      return blocks;
                    })()}
                  </div>
                );
              });
            })()}
            {messages.length === 0 && approvals.length === 0 && (
              <div className="text-center text-muted-foreground text-sm p-8">
                {t('workbench.noConversationYet')}
              </div>
            )}
            {/* ADR-030 · 用户刚操作(发消息/点审批)后或 tick 运行中，且尚无 token 直播（如 LLM 思考期）→
                在 AI 回复位置显示动态 loading 气泡，避免用户误以为程序卡死；首个新 AI 输出/token 直播/
                超时即自动消失。awaitingReply 补足 SSE is_running 投递延迟的空窗。
                ⚠️ 但**暂停等人**时（有未决审批卡 / paused_for_human）绝不显示——此刻 AI 在等你，不是在跑。 */}
            {(awaitingReply || streamState.is_running)
              && !approvals.some((a) => !a.resolution)
              && !['paused_clarification', 'paused_waiting_capability'].includes(
                streamState.lifecycle_status || project?.lifecycle_status || '',
              )
              && !messages.some((m) => m.id === 'stream-live') && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground px-3 py-2 rounded-[14px] bg-muted/40 w-fit my-1">
                <Loader2 className="w-4 h-4 animate-spin text-primary" />
                <span>{t('workbench.assistantThinking')}</span>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>

          {/* 输入框：worker/health 线程只读（ADR-024 #9）；仅主线可发消息 */}
          {(() => {
            const _kind = threads.find((th) => th.thread_key === activeThreadKey)?.thread_kind;
            const _readOnly = _kind === 'super_worker_thread' || _kind === 'worker_health';
            if (_readOnly) {
              return (
                <div className="border-t border-border bg-card p-3 text-center text-[11px] text-muted-foreground italic">
                  {t('workbench.readOnlyThread')}
                </div>
              );
            }
            return (
              <div className="border-t border-border bg-card">
                {feedback && (
                  <div className="px-3 pt-2 text-xs text-primary bg-primary/10 border-b border-border">
                    {feedback}
                  </div>
                )}
                <div className="p-3">
                  <InputBox
                    value={input}
                    onChange={setInput}
                    onSubmit={() => void send()}
                    // 运行中（tick 进行时）锁输入：发消息会 cancel 当前 tick + 重跑 → 造成 plan 抖动/500。
                    // 想打断请用下方「停止/打断」按钮（始终可用）。
                    disabled={sending || streamState.is_running}
                    onAddFiles={(files) => void addFiles(files)}
                    attachments={attachments}
                    onRemoveAttachment={removeAttachment}
                    placeholder={t('workbench.inputPlaceholder')}
                  />
                  <div className="flex justify-between items-center mt-1">
                    <p className="text-[10px] text-muted-foreground">
                      💡 {t('workbench.inputHint', { count: streamState.pending_count ?? 0 })}
                    </p>
                    {streamState.is_running && (
                      <Button size="sm" variant="outline" onClick={() => void interrupt()}>
                        <StopCircle className="w-3.5 h-3.5 mr-1" /> {t('workbench.interruptTick')}
                      </Button>
                    )}
                  </div>
                </div>
              </div>
            );
          })()}
        </main>

        {/* 右栏 — v5 · 4 tabs (Activity / Schedule / Memory / Goal) */}
        <aside className="w-80 border-l border-border overflow-y-auto bg-card flex flex-col">
          <div className="border-b border-border flex">
            {([
              ['activity', `🔴 ${t('workbench.tabActivity')}`, !!liveCalls.length],
              ['threads', `🧵 ${t('workbench.tabThreads')}`],
              ['schedule', `📅 ${t('workbench.tabSchedule')}`],
              ['memory', `🧠 ${t('workbench.tabMemory')}`],
            ] as Array<[RightTab, string, boolean?]>).map(([key, label, hasNew]) => (
              <button
                key={key}
                className={`flex-1 px-2 py-2 text-xs ${
                  rightTab === key ? 'border-b-2 border-primary font-semibold bg-accent' : 'text-muted-foreground'
                }`}
                onClick={() => setRightTab(key)}
              >
                {label}{hasNew ? ' •' : ''}
              </button>
            ))}
          </div>
          <div className="flex-1 overflow-y-auto p-3 text-sm space-y-3">
            {rightTab === 'activity' && (
              <>
                {/* V7.4 · ActivityTree 已退役（ADR-007）；观测看左栏 chat 时间线 */}
                <section>
                  <div className="text-xs font-semibold text-muted-foreground mb-1">{t('workbench.mission')}</div>
                  <div className="font-mono text-xs break-all">{project?.mission_id}</div>
                  <div className="text-[10px] text-muted-foreground">slug: {slug}</div>
                </section>
                <hr className="border-border" />
                {/* v5 · 实时 worker 调用流 (legacy；与 ActivityTree 重叠，保留作 fallback) */}
                <section>
                  <div className="text-xs font-semibold mb-1">⚡ {t('workbench.liveWorkerCalls', { count: liveCalls.length })}</div>
                  {liveCalls.length === 0 ? (
                    <div className="text-xs text-muted-foreground italic">{t('workbench.noWorkerCalls')}</div>
                  ) : (
                    <div className="space-y-1.5 max-h-72 overflow-auto">
                      {liveCalls.slice().reverse().map((c) => (
                        <div key={c.call_id} className={`border border-border rounded p-1.5 text-[11px] ${c.done ? 'bg-muted/50' : 'bg-primary/10 border-primary/30'}`}>
                          <div className="font-mono truncate">
                            {c.capability || '?'}<span className="text-muted-foreground">.{c.action || '?'}</span>
                          </div>
                          <div className="flex items-center gap-1 mt-0.5">
                            <span>{c.stage}</span>
                            {c.duration_ms != null && (
                              <span className="text-muted-foreground ml-auto">{c.duration_ms}ms</span>
                            )}
                          </div>
                          {c.error_msg && (
                            <div className="text-[10px] text-destructive mt-0.5 line-clamp-2">{c.error_msg}</div>
                          )}
                          {c.artifact_url && (
                            <a href={c.artifact_url} target="_blank" rel="noreferrer" className="text-primary underline text-[10px]">
                              📎 artifact
                            </a>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </section>
                <hr className="border-border" />
                <section>
                  <div className="text-xs font-semibold mb-1">{t('workbench.callStats7d')}</div>
                  {stats && Object.keys(stats.by_status || {}).length > 0 ? (
                    <div className="space-y-1">
                      {Object.entries(stats.by_status).map(([st, v]) => (
                        <div key={st} className="flex justify-between text-xs">
                          <span>{st}</span>
                          <span>{v.cnt}</span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="text-xs text-muted-foreground">{t('workbench.noData')}</div>
                  )}
                </section>
                <hr className="border-border" />
                <section>
                  <div className="text-xs font-semibold mb-1">{t('workbench.recentErrors')}</div>
                  {(stats?.top_errors?.length ?? 0) > 0 ? (
                    <ul className="text-[10px] space-y-0.5">
                      {stats!.top_errors.slice(0, 5).map((e, i) => (
                        <li key={i}>×{e.cnt} · <code>{(e.err || '').slice(0, 60)}</code></li>
                      ))}
                    </ul>
                  ) : (
                    <div className="text-xs text-muted-foreground">{t('workbench.none')}</div>
                  )}
                </section>
              </>
            )}
            {rightTab === 'schedule' && project?.mission_id && (
              <ScheduleEditor projectId={project.mission_id} />
            )}
            {rightTab === 'memory' && <MemoryTab slug={slug} />}
            {rightTab === 'threads' && (
              <section className="space-y-1.5">
                <div className="text-[11px] text-muted-foreground italic">{t('workbench.workerThreadsHint')}</div>
                {threads.filter((th) => th.thread_kind === 'super_worker_thread').length === 0 && (
                  <div className="text-xs text-muted-foreground italic">{t('workbench.noWorkerThreads')}</div>
                )}
                {threads.filter((th) => th.thread_kind === 'super_worker_thread').map((th) => {
                  const isActive = activeThreadKey === th.thread_key;
                  return (
                    <div
                      key={th.thread_key}
                      onClick={() => switchToThread(th.thread_key)}
                      className={`group relative px-2 py-1.5 rounded border text-xs cursor-pointer ${isActive ? 'border-primary bg-accent' : 'border-border hover:bg-muted/50'}`}
                    >
                      <div className="font-medium truncate pr-5">{th.title?.trim() || th.thread_key}</div>
                      <div className="text-[10px] text-muted-foreground mt-0.5">
                        {t('workbench.msgCount', { count: th.msg_count })} · {th.last_msg_at?.slice(0, 16) || '-'}
                      </div>
                      <button
                        title={t('workbench.clearWorkerThreadHint')}
                        className="absolute top-1 right-1 opacity-0 group-hover:opacity-100 p-0.5 text-destructive hover:bg-destructive/10 rounded"
                        onClick={async (e) => {
                          e.stopPropagation();
                          if (!(await confirm({ message: t('workbench.clearWorkerThreadConfirm', { label: th.title || th.thread_key }), danger: true }))) return;
                          try {
                            await observeV3Api.deleteThread(slug, th.thread_key);
                            if (activeThreadKey === th.thread_key) setActiveThreadKey('main');
                            await refreshAll();
                          } catch (err) {
                            setErr(errMessage(err));
                          }
                        }}
                      >
                        <Trash2 className="w-3 h-3" />
                      </button>
                    </div>
                  );
                })}
              </section>
            )}
            {/* 🎯 目标 tab 已删：goal_spec 运行时未被读取（super 目标由 soul_md 驱动），是空壳无实际作用 */}
            {/* ADR-009 G5 · Builder 工作记录（自身有 mutation 记录时才显示；非 Builder 自然为空） */}
            <BuilderWorkLogPanel slug={slug} />
          </div>
        </aside>
      </div>

      {/* New mission (instance) dialog */}
      <Dialog open={newMissionOpen} onClose={() => setNewMissionOpen(false)} title={t('workbench.newMissionTitle')}>
        <div className="space-y-3">
          <p className="text-xs text-muted-foreground">{t('workbench.newMissionDesc')}</p>
          <div className="space-y-1">
            <label className="text-[11px] text-muted-foreground">{t('workbench.newMissionNameLabel')}</label>
            <input
              value={newMissionName}
              onChange={(e) => setNewMissionName(e.target.value)}
              placeholder={t('workbench.newMissionNamePlaceholder')}
              className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground"
            />
          </div>
          <div className="space-y-1">
            <label className="text-[11px] text-muted-foreground">{t('workbench.newMissionHintLabel')}</label>
            <input
              value={newMissionHint}
              onChange={(e) => setNewMissionHint(e.target.value)}
              placeholder={t('workbench.newMissionHintPlaceholder')}
              className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground"
            />
          </div>
          <div className="flex justify-end gap-2 pt-1">
            <Button size="sm" variant="outline" onClick={() => setNewMissionOpen(false)} disabled={spawning}>
              {t('common.cancel')}
            </Button>
            <Button size="sm" onClick={() => void spawnMission()} disabled={spawning || !newMissionName.trim()}>
              {spawning ? t('superRole.createAndEnter') : t('superRole.createAndEnterCta')}
            </Button>
          </div>
        </div>
      </Dialog>
    </div>
  );
}
