import {afterEach, describe, expect, it} from "vitest";
import ts from "typescript";
// @ts-expect-error Vite raw asset import; not part of the Worker runtime.
import htmlSource from "../app/static/index.html?raw";
// @ts-expect-error Vite raw asset import.
import appSource from "../app/static/app.js?raw";
// @ts-expect-error Vite raw asset import.
import checkoutSource from "../app/static/checkout.js?raw";
// @ts-expect-error Shared browser module, without a bundler or runtime dependency.
import {translations, t, setLanguage, localizedError} from "../app/static/console-locale.js";

afterEach(() => setLanguage("en"));
const han = /[\u3400-\u9fff]/;

describe("console localization", () => {
  it("covers every static HTML label and every Chinese JavaScript message", () => {
    const html: string = htmlSource;
    const labels = [...html.matchAll(/>([^<>]+)</g)].map((m) => m[1].trim());
    labels.push(...[...html.matchAll(/(?:aria-label|placeholder)="([^"]+)"/g)].map((m) => m[1]));
    for (const label of labels.filter((value) => han.test(value))) expect(translations, label).toHaveProperty(label);
    for (const [file, contents] of [["app/static/app.js", appSource], ["app/static/checkout.js", checkoutSource]]) {
      const source = ts.createSourceFile(file, contents, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
      function visit(node: ts.Node) {
        let key = "";
        if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) key = node.text;
        if (ts.isTemplateExpression(node)) {
          key = node.head.text;
          node.templateSpans.forEach((span, index) => {key += `{${index}}${span.literal.text}`;});
        }
        if (han.test(key)) expect(translations, key).toHaveProperty(key);
        ts.forEachChild(node, visit);
      }
      visit(source);
    }
  });

  it("has English translations with identical interpolation slots", () => {
    for (const [source, translated] of Object.entries(translations) as [string, string][]) {
      expect(han.test(translated), source).toBe(false);
      expect([...translated.matchAll(/\{\d+\}/g)].map((m) => m[0]).sort(), source)
        .toEqual([...source.matchAll(/\{\d+\}/g)].map((m) => m[0]).sort());
    }
  });

  it("switches language without translating or recursively interpolating customer content", () => {
    expect(t("登录")).toBe("Sign in");
    const name = "客户名称 {1} <script>";
    expect(t`确认吊销 ${name}？此操作立即生效。`).toBe(`Revoke ${name}? This takes effect immediately.`);
    setLanguage("zh");
    expect(t("登录")).toBe("登录");
    expect(t`确认吊销 ${name}？此操作立即生效。`).toBe(`确认吊销 ${name}？此操作立即生效。`);
    setLanguage("unexpected");
    expect(t("登录")).toBe("Sign in");
  });

  it("gives safe English error guidance without guessing unknown server details", () => {
    expect(han.test(localizedError("邮箱或密码错误", 401))).toBe(false);
    expect(localizedError("余额或者预算不足", 402)).toMatch(/credit|budget/i);
    expect(localizedError("未知中文服务器错误", 503)).toContain("HTTP 503");
    setLanguage("zh");
    expect(localizedError("未知中文服务器错误", 503)).toBe("未知中文服务器错误");
  });
});
