/**
 * MarkdownViewer · data:image QR 渲染回归（2026-07-03 e2e 实证缺陷）。
 *
 * 事故：readiness QR 卡正文是 `![登录二维码](data:image/png;base64,...)`，
 * react-markdown 的 defaultUrlTransform 在 rehype-sanitize **之前**就把非
 * http(s) 协议掐成空串 → <img src=""> 白图。SANITIZE_SCHEMA 里加的 data:
 * 白名单（ADR-012 R4）从没生效过。修：urlTransform 放行 data:image/*。
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { MarkdownViewer } from './markdown-viewer';

const QR_PNG =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';

describe('MarkdownViewer data:image', () => {
  it('renders base64 QR image with src preserved', () => {
    render(<MarkdownViewer content={`![登录二维码](${QR_PNG})`} />);
    const img = screen.getByAltText('登录二维码') as HTMLImageElement;
    expect(img.getAttribute('src')).toBe(QR_PNG);
  });

  it('still strips dangerous protocols', () => {
    render(<MarkdownViewer content={`![x](javascript:alert(1))`} />);
    const img = screen.getByAltText('x') as HTMLImageElement;
    expect(img.getAttribute('src') || '').not.toContain('javascript');
  });

  it('keeps normal https images working', () => {
    render(<MarkdownViewer content={`![pic](https://example.com/a.png)`} />);
    const img = screen.getByAltText('pic') as HTMLImageElement;
    expect(img.getAttribute('src')).toBe('https://example.com/a.png');
  });
});
