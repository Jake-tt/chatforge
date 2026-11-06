import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import Markdown from "./Markdown";

const html = (text: string, streaming = false) =>
  renderToStaticMarkup(<Markdown text={text} streaming={streaming} />);

const DIAGRAM = "```mermaid\ngraph TD\n  A-->B\n```";

describe("Markdown", () => {
  it("renders a markdown table as a table", () => {
    const out = html("| 요금제 | 한도 |\n|---|---|\n| free | 200,000 |");
    expect(out).toContain("<table>");
    expect(out).toContain("<th>요금제</th>");
    expect(out).toContain("<td>200,000</td>");
  });

  it("never renders HTML the model wrote", () => {
    const out = html(
      '<script>alert(1)</script>\n\n문장 속 <img src=x onerror="alert(1)"> 태그\n\n**굵게**',
    );
    expect(out).not.toContain("<script");
    expect(out).not.toContain("<img");
    expect(out).toContain("문장 속  태그"); // the inline tag is dropped, its sentence kept
    expect(out).toContain("<strong>굵게</strong>");
  });

  it("drops javascript: links and opens the rest in a new tab", () => {
    expect(html("[클릭](javascript:alert(1))")).not.toContain("javascript:");
    const out = html("[문서](https://example.com/help)");
    expect(out).toContain('href="https://example.com/help"');
    expect(out).toContain('target="_blank"');
    expect(out).toContain('rel="noopener noreferrer"');
  });

  it("shows a diagram as code while the answer is still streaming", () => {
    const out = html(DIAGRAM, true);
    expect(out).toContain("<pre><code");
    expect(out).toContain("graph TD");
    expect(out).not.toContain('class="diagram"');
  });

  it("draws a diagram once the answer is complete", () => {
    const out = html(DIAGRAM);
    expect(out).toContain('class="diagram"');
  });
});
