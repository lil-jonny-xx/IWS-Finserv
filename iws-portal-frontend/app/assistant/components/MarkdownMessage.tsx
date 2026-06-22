'use client';

// Renders an assistant message as Markdown (GFM: tables, lists, etc.).
// Tailwind's preflight strips default element styling and there's no typography
// plugin, so every tag is mapped explicitly to the assistant's theme tokens
// (text-ink / text-dim / border-rule / bg-card / text-prime).
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Components } from 'react-markdown';

const components: Components = {
  h1: ({ children }) => <h1 className="mt-4 mb-2 text-base font-semibold text-ink">{children}</h1>,
  h2: ({ children }) => <h2 className="mt-4 mb-2 text-base font-semibold text-ink">{children}</h2>,
  h3: ({ children }) => <h3 className="mt-3 mb-1.5 text-sm font-semibold text-ink">{children}</h3>,
  p: ({ children }) => <p className="my-2 leading-relaxed">{children}</p>,
  ul: ({ children }) => <ul className="my-2 list-disc space-y-1 pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="my-2 list-decimal space-y-1 pl-5">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  strong: ({ children }) => <strong className="font-semibold text-ink">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="text-prime underline underline-offset-2">
      {children}
    </a>
  ),
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 border-rule pl-3 italic text-dim">{children}</blockquote>
  ),
  hr: () => <hr className="my-3 border-rule" />,
  pre: ({ children }) => (
    <pre className="my-2 overflow-x-auto rounded-lg bg-card p-3 text-xs">{children}</pre>
  ),
  code: ({ className, children }) => {
    // Fenced blocks carry a language- class and live inside <pre> (no extra bg);
    // bare inline code gets a subtle chip.
    if (/language-/.test(className || '')) {
      return <code className={`${className} font-mono`}>{children}</code>;
    }
    return <code className="rounded bg-card px-1 py-0.5 font-mono text-[0.85em] text-ink">{children}</code>;
  },
  table: ({ children }) => (
    <div className="my-3 overflow-x-auto">
      <table className="w-full border-collapse text-xs">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border-b border-rule px-2 py-1.5 text-left font-semibold text-ink">{children}</th>
  ),
  td: ({ children }) => (
    <td className="border-b border-rule/40 px-2 py-1.5 align-top">{children}</td>
  ),
};

export default function MarkdownMessage({ children }: { children: string }) {
  return (
    <div className="text-sm text-ink">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
