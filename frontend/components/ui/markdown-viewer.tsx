'use client';

import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import { renderToStaticMarkup } from 'react-dom/server';
import remarkGfm from 'remark-gfm';
import rehypeSanitize from 'rehype-sanitize';
import { defaultSchema } from 'hast-util-sanitize';
import { cn } from '@/lib/utils';

const SANITIZE_SCHEMA = {
  ...defaultSchema,
  tagNames: [...(defaultSchema.tagNames ?? []), 'img'],
  attributes: {
    ...(defaultSchema.attributes ?? {}),
    img: ['src', 'alt', 'title', 'width', 'height', 'loading'],
  },
  // ADR-012 R4 · 允许 data: 协议的 img src —— 让 agent 直接嵌 base64 图（如二维码）。
  // 仅放开 src（图片）；href 等不放开，data:text/html 在 <img src> 里不会执行，安全。
  protocols: {
    ...(defaultSchema.protocols ?? {}),
    src: [...((defaultSchema.protocols?.src as string[]) ?? ['http', 'https']), 'data'],
  },
};

// react-markdown 的 defaultUrlTransform 在 rehype-sanitize **之前**跑，会把非 http(s)
// 协议掐成空串——上面 SANITIZE_SCHEMA 给 img src 放行的 data:（ADR-012 R4，QR base64）
// 因此从未生效（2026-07-03 QR 卡白图实证）。这里放行 data:image/*，其余仍走默认转换；
// data:image 在 <img src> 不执行脚本，且 sanitize 仍限定只有 img.src 收 data: 协议。
function urlTransform(url: string): string {
  if (url.startsWith('data:image/')) return url;
  return defaultUrlTransform(url);
}

export function renderMarkdownToHtml(content: string): string {
  return renderToStaticMarkup(
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[[rehypeSanitize, SANITIZE_SCHEMA]]}
      urlTransform={urlTransform}
    >
      {content || '(无内容)'}
    </ReactMarkdown>,
  );
}

export function MarkdownViewer({
  content,
  className,
}: {
  content: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        'prose prose-sm max-w-none overflow-x-auto text-foreground',
        'prose-headings:font-semibold prose-headings:text-foreground',
        'prose-p:text-foreground prose-strong:text-foreground',
        'prose-a:text-primary hover:prose-a:text-primary/80',
        // inline code: muted bg + clear foreground 文字（默认主题色对比够）
        'prose-code:rounded prose-code:bg-muted prose-code:px-1 prose-code:py-0.5 prose-code:text-foreground prose-code:font-medium',
        // 代码块：浅灰底 + 深色文字（不再用 prose 默认的灰得发白文字）
        'prose-pre:overflow-x-auto prose-pre:rounded-lg prose-pre:border prose-pre:border-border prose-pre:bg-muted/70 prose-pre:text-foreground',
        // pre > code 取消 inline code 的 bg/padding 样式（否则代码块里又裹一层灰底）
        '[&_pre>code]:bg-transparent [&_pre>code]:p-0 [&_pre>code]:text-foreground [&_pre>code]:font-mono',
        'prose-blockquote:border-l-primary prose-blockquote:bg-muted/40 prose-blockquote:py-1 prose-blockquote:pl-3',
        'prose-th:border prose-th:border-border prose-th:bg-muted/60 prose-th:px-2 prose-th:py-1',
        'prose-td:border prose-td:border-border prose-td:px-2 prose-td:py-1',
        'prose-img:rounded-lg prose-img:my-2',
        className,
      )}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeSanitize, SANITIZE_SCHEMA]]}
        urlTransform={urlTransform}
      >
        {content || '(无内容)'}
      </ReactMarkdown>
    </div>
  );
}
