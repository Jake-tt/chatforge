import type { Element } from "hast";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { renderDiagram } from "../diagram";

interface Props {
  text: string;
  streaming: boolean;
}

// The text is model output. HTML in it is dropped (skipHtml) and react-markdown's default
// URL transform removes javascript: and similar links; diagrams render in strict mode.
export default function Markdown({ text, streaming }: Props) {
  const components: Components = {
    a: ({ href, children }) => (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    ),
    pre: ({ node, children }) => {
      const source = mermaidSource(node);
      // Half a diagram doesn't parse, so it is drawn only once the answer is complete.
      if (source !== null && !streaming) return <Diagram source={source} />;
      return <pre>{children}</pre>;
    },
  };
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

function mermaidSource(pre: Element | undefined): string | null {
  const code = pre?.children[0];
  if (code?.type !== "element" || code.tagName !== "code") return null;
  const classes = code.properties.className;
  if (!Array.isArray(classes) || !classes.includes("language-mermaid")) return null;
  return code.children.map((c) => (c.type === "text" ? c.value : "")).join("");
}

function Diagram({ source }: { source: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    renderDiagram(source).then(
      (svg) => {
        if (!cancelled && ref.current) ref.current.innerHTML = svg;
      },
      () => {
        if (!cancelled) setFailed(true);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [source]);

  if (failed) {
    return (
      <>
        <pre>
          <code>{source}</code>
        </pre>
        <div className="note">도식을 그리지 못해 원문을 보여드려요.</div>
      </>
    );
  }
  return <div className="diagram" ref={ref} role="img" aria-label="도식" />;
}
