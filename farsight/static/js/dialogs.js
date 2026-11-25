// Add / edit / remove node dialogs.
import { h, icon, api, toast } from "./util.js";
import { t } from "./i18n.js";

function field(label, input, hint) {
  return h("label", { class: "field" }, label, input, hint ? h("small", { text: hint }) : null);
}

/**
 * `node` = existing node summary for editing, or null to add. `preset` may carry { hosts } to prefill.
 * `defaults.user` is the user name suggested for new nodes.
 */
export function nodeDialog({ node = null, preset = {}, defaults = {} } = {}) {
  const editing = Boolean(node);
  let auth = node ? node.auth : "key";
  const hosts = editing
    ? h("input", { name: "host", value: node.host, required: true, autocomplete: "off", spellcheck: "false" })
    : h("textarea", { name: "hosts", required: true, autocomplete: "off", spellcheck: "false", placeholder: "10.0.0.24\n10.0.0.[17,25]\nroot@gpu-a:2222" });
  if (!editing && preset.hosts) hosts.value = preset.hosts;
  const user = h("input", { name: "user", value: node ? node.user : (defaults.user || "root"), required: true, autocomplete: "off", spellcheck: "false" });
  const port = h("input", { name: "port", type: "number", min: "1", max: "65535", value: String(node ? node.port : 22), required: true });
  const password = h("input", { name: "password", type: "password", autocomplete: "off", placeholder: editing ? t("留空则沿用已输入的密码") : "" });
  const keyPath = h("input", { name: "key_path", value: (node && node.key_path) || "", autocomplete: "off", spellcheck: "false", placeholder: t("留空：使用 ~/.ssh 下的默认密钥、ssh-agent 与 ~/.ssh/config") });
  const label = h("input", { name: "label", value: (node && node.label) || "", maxlength: "60", autocomplete: "off", placeholder: t("可选，例如 Prefill 机柜 A") });
  const passwordField = field(t("密码"), password, t("只保存在面板进程的内存里，不写入磁盘；面板重启后需要重新输入。"));
  const keyField = field(t("私钥路径"), keyPath, t("路径指面板所在机器上的文件。带口令的私钥请先加入 ssh-agent。"));
  const ssh = [["key", t("密钥 / Agent")], ["password", t("密码")]];
  const modes = !editing ? ssh.concat([["local", t("本机")]]) : node.auth === "local" ? [["local", t("本机")]] : ssh;
  const seg = h("div", { class: "seg", role: "group", "aria-label": t("连接方式") }, modes.map(([key, text]) =>
    h("button", { type: "button", "aria-pressed": String(key === auth), text, onclick: () => { auth = key; refresh(); } })));
  const localNote = h("div", { class: "field" }, h("small", { text: t("监控面板所在的这台机器：直接在本机运行采集器，不经过 SSH。") }));
  const error = h("div", { class: "form-error", role: "alert", hidden: true });
  const submit = h("button", { class: "btn primary", type: "submit" }, editing ? t("保存并重连") : t("添加并连接"));
  function refresh() {
    Array.from(seg.children).forEach((button, index) => button.setAttribute("aria-pressed", String(modes[index][0] === auth)));
    passwordField.hidden = auth !== "password";
    keyField.hidden = auth !== "key";
    localNote.hidden = auth !== "local";
    hostField.hidden = targetRow.hidden = auth === "local";
  }
  const hostField = field(editing ? t("主机地址") : t("主机地址（可一次填多个）"), hosts, editing ? null : t("IP 或主机名，用空格、逗号或换行分隔；支持 10.0.0.[1-8] 这样的范围，以及 用户@主机:端口。"));
  const targetRow = h("div", { class: "field-row" }, field(t("用户名"), user), field(t("SSH 端口"), port));
  const form = h("form", { class: "sheet-body", novalidate: true },
    h("div", { class: "field" }, t("连接方式"), seg), hostField, targetRow, passwordField, keyField, localNote, field(t("备注"), label), error,
    h("div", { class: "sheet-actions" }, h("button", { class: "btn", type: "button", onclick: () => dialog.close() }, t("取消")), submit));
  const dialog = h("dialog", { class: "sheet", "aria-label": editing ? t("编辑节点") : t("添加节点") },
    h("div", { class: "sheet-head" }, h("div", null, h("h2", { text: editing ? t("编辑节点") : t("添加节点") }),
      h("p", { text: editing ? t("保存后会立即用新的设置重新连接。") : t("添加后会立即尝试 SSH 连接；连不上时卡片上会写明原因。") })),
      h("button", { class: "back", type: "button", "aria-label": t("关闭"), onclick: () => dialog.close() }, icon("x"))),
    form);
  refresh();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    const body = { user: user.value.trim(), port: Number(port.value), auth, label: label.value.trim() };
    if (editing) body.host = hosts.value.trim(); else body.hosts = hosts.value;
    if (auth === "password" && password.value) body.password = password.value;
    if (auth === "key") body.key_path = keyPath.value.trim() || null;
    if (auth !== "local" && !(editing ? body.host : body.hosts.trim())) { error.textContent = t("请填写主机地址"); error.hidden = false; hosts.focus(); return; }
    if (auth === "password" && !editing && !password.value) { error.textContent = t("请填写密码"); error.hidden = false; password.focus(); return; }
    submit.disabled = true;
    try {
      if (editing) {
        await api(`/api/nodes/${node.id}`, { method: "PATCH", body });
        toast(t("已保存，正在重新连接 {host}", { host: body.host }));
      } else {
        const result = await api("/api/nodes", { method: "POST", body });
        toast(t("已添加 {n} 个节点，正在连接", { n: result.created.length }) + (result.skipped.length ? t("；{n} 个已存在", { n: result.skipped.length }) : ""));
      }
      dialog.close();
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  if (auth !== "local") (editing && auth === "password" ? password : hosts).focus();
  return dialog;
}

export function confirmRemove(node, done) {
  const dialog = h("dialog", { class: "sheet", "aria-label": t("移除节点") },
    h("div", { class: "sheet-head" }, h("div", null, h("h2", { text: t("移除 {name}？", { name: node.name }) }),
      h("p", { text: t("面板会断开与该节点的连接并删除它的历史曲线。节点上的服务和数据不受任何影响。") }))),
    h("div", { class: "sheet-body" }, h("div", { class: "sheet-actions" },
      h("button", { class: "btn", type: "button", onclick: () => dialog.close() }, t("取消")),
      h("button", { class: "btn primary", type: "button", onclick: async () => {
        try { await api(`/api/nodes/${node.id}`, { method: "DELETE", body: {} }); toast(t("已移除 {name}", { name: node.name })); dialog.close(); if (done) done(); }
        catch (failure) { toast(failure.message); }
      } }, t("移除")))));
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}
