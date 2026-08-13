// Mermaid 快速语法检查：只做 mermaid.parse，不启动 Chromium 渲染。
// 用法: node scripts/mermaid_parse.mjs <file.mmd>
// 退出码: 0=语法合法；1=语法错误（错误信息打印到 stderr）；2=预检自身故障（调用方应跳过预检）
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!DOCTYPE html><html><body></body></html>", {
  pretendToBeVisual: true,
});
for (const key of ["window", "document", "navigator", "Element", "Node"]) {
  Object.defineProperty(globalThis, key, {
    value: dom.window[key],
    configurable: true,
    writable: true,
  });
}

try {
  const { default: mermaid } = await import("mermaid");
  const code = readFileSync(process.argv[2], "utf-8");
  await mermaid.parse(code);
  process.exit(0);
} catch (err) {
  if (err && typeof err.message === "string") {
    console.error(err.message);
    process.exit(1);
  }
  console.error(String(err));
  process.exit(2);
}
