// Two UI languages. The source language is Chinese: `t("…{n}…", { n })` returns the text itself in Chinese and
// its entry in EN in English. A string that is translated later (a table of labels) is marked with N("…") so the
// tests can check that every source string has an English entry. Text coming from the server is already in the
// language of the request (the X-Lang header set in util.js `api`).
import { EN } from "./i18n-en.js";

const KEY = "farsight-lang";

function initial() {
  let stored = null;
  try { stored = localStorage.getItem(KEY); } catch (error) { /* storage may be blocked */ }
  if (stored === "zh" || stored === "en") return stored;
  return (navigator.language || "zh").toLowerCase().startsWith("zh") ? "zh" : "en";
}

let lang = initial();
document.documentElement.lang = lang === "en" ? "en" : "zh-CN";

export const getLang = () => lang;

export function setLang(next) {
  lang = next === "en" ? "en" : "zh";
  try { localStorage.setItem(KEY, lang); } catch (error) { /* storage blocked: the choice lasts for this page */ }
  document.documentElement.lang = lang === "en" ? "en" : "zh-CN";
}

/** Translate a source string. An English entry may be [singular, plural], chosen by `params.n`. */
export function t(text, params) {
  let out = text;
  if (lang === "en") {
    const entry = EN[text];
    if (Array.isArray(entry)) out = entry[params && Number(params.n) === 1 ? 0 : 1];
    else if (entry !== undefined) out = entry;
  }
  return params ? out.replace(/\{(\w+)\}/g, (match, key) => (params[key] == null ? "" : String(params[key]))) : out;
}

/** Marks a source string that is translated where it is used. */
export const N = (text) => text;
