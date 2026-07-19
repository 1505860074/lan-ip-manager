// ===== 通用小工具 =====

// 发一个 POST 请求，body 是对象，自动转 JSON，返回解析好的 JSON。
async function post(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return r.json();
}

// 按 name 读取页面上某个输入框的值（② 和 ③ 里的字段都靠它取）。
function getVal(name) {
  const el = document.querySelector(`[name="${name}"]`);
  return el ? el.value.trim() : "";
}

// 把纯文本里的 < > & 转义掉，避免把命令输出当成 HTML 渲染（安全 & 显示正确）。
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// 收集 ② 里的一套 SSH 连接信息；缺关键项返回 null。改 IP / 测试连接都复用它。
// iface = 本机直连网口：本机和对方不同网段时，后端要靠它自动临时加/删 IP。
function collectConn() {
  const conn = {
    peer_ip: getVal("peer_ip"),
    port: getVal("port") || "22",
    username: getVal("username"),
    password: getVal("password"),
    sudo_password: getVal("sudo_password"),
    iface: getVal("iface"),
  };
  if (!conn.peer_ip || !conn.username || !conn.password) return null;
  return conn;
}

// 把一次「远端执行」的结果（退出码 + 标准输出 + 标准错误）渲染到指定容器。
function renderExec(out, r) {
  if (!r.ok) {
    out.innerHTML = `<span class="err">错误：${escapeHtml(r.error)}</span>`;
    return;
  }
  let html = `<div class="rc ${r.rc === 0 ? "ok" : "bad"}">退出码 ${r.rc} ${
    r.rc === 0 ? "（成功）" : "（异常）"
  }</div>`;
  if (r.stdout) html += `<pre class="std">${escapeHtml(r.stdout)}</pre>`;
  if (r.stderr) html += `<pre class="stderr">${escapeHtml(r.stderr)}</pre>`;
  if (!r.stdout && !r.stderr) html += `<pre class="std">（命令没有任何输出）</pre>`;
  out.innerHTML = html;
}

// ===== ① 网口列表 =====
async function loadIfaces() {
  const r = await (await fetch("/api/interfaces")).json();
  // 顶部 root 警告：不是 root 才显示
  document.getElementById("rootWarn").classList.toggle("hidden", r.root);

  const tb = document.querySelector("#ifaceTable tbody");
  tb.innerHTML = "";
  if (!r.ok) {
    tb.innerHTML = `<tr><td colspan="5">错误：${r.error}</td></tr>`;
    return;
  }
  for (const i of r.interfaces) {
    const tr = document.createElement("tr");
    if (i.carrier) tr.classList.add("connected"); // 已连网口整行高亮，一眼看到
    // 插了网线才能扫描；灰按钮也带上 title 说明为什么不能点
    const scanBtn = i.carrier
      ? `<button class="btn" title="在这个网口上监听，找出直连的对方设备；同时把它自动填进第 ② 步的「本机直连网口」。" onclick="scan('${i.name}')">扫描找设备</button>`
      : `<button class="btn" disabled title="该口未插网线（未连），无法扫描。">扫描找设备</button>`;
    tr.innerHTML = `
      <td>${i.name}</td>
      <td>${i.carrier ? "<b>已连</b>" : "未连"}</td>
      <td>${i.operstate}</td>
      <td>${i.addresses.join("<br>") || "-"}</td>
      <td>${scanBtn}</td>`;
    tb.appendChild(tr);
  }
}

// 把某网口名填进第 ② 步的「本机直连网口」框
function useIface(name) {
  document.querySelector("[name=iface]").value = name;
}

// ===== ① 持续扫描：后台一直抓包，前端每秒刷新一次结果 =====
let scanTimer = null; // setInterval 句柄
let scanStartMs = 0; // 本轮开始时刻，用于 3 分钟安全上限
const SCAN_MAX_MS = 180000; // 连续扫描最多 3 分钟，避免忘了停

// 网口表里每行「扫描找设备」按钮调用它：开始（或切换到）在该口上持续扫描。
async function scan(iface) {
  useIface(iface); // 扫描用的口，也是之后连对方时自动临时打通要用的本机口，顺手填上
  stopScanTimer(); // 若正在扫别的口，先停掉前端轮询
  const s = document.getElementById("scanStatus");
  s.textContent = `正在 ${iface} 上开始持续监听……`;

  const r = await post("/api/scan_start", { iface });
  if (!r.ok) {
    s.textContent = "无法开始扫描：" + r.error;
    return;
  }
  scanStartMs = performance.now();
  document.getElementById("stopScanBtn").classList.remove("hidden");
  pollScan(); // 立刻拉一次
  scanTimer = setInterval(pollScan, 1000); // 之后每秒一次
}

// 每秒调一次：取后端累积到的当前快照并重绘表格。
async function pollScan() {
  let r;
  try {
    r = await (await fetch("/api/scan_poll")).json();
  } catch (e) {
    return; // 偶发网络抖动，忽略这一拍，下一秒再试
  }
  if (!r.ok) return;
  renderDevices(r.devices);

  const n = r.devices.length;
  const s = document.getElementById("scanStatus");
  let msg = `监听中……（每秒刷新）当前在线 ${n} 台设备`;
  if (r.hidden_public) msg += `，已隐藏 ${r.hidden_public} 条经网关路过的公网流量`;
  if (n === 0) msg += "（还没抓到广播包：确认网线已插、对方已开机，稍等片刻）";
  s.textContent = msg;

  // 安全上限：连扫超过 3 分钟自动停，省得后台一直抓
  if (performance.now() - scanStartMs > SCAN_MAX_MS) {
    stopScan();
    s.textContent = `已自动停止（连扫超过 3 分钟）。共发现 ${n} 台设备；再点「扫描找设备」可重新开始。`;
  }
}

// 把设备列表渲染进结果表（每次全量重绘，表很小，不卡）。
function renderDevices(devices) {
  const tb = document.querySelector("#devTable tbody");
  tb.innerHTML = "";
  for (const d of devices) {
    const tr = document.createElement("tr");
    if (d.is_gateway) tr.classList.add("gateway"); // 疑似网关的行淡化显示
    // 来源标签：ARP = 设备自己声明的 IP，可信；IP包 = 凑出来的，仅供参考
    const via =
      d.via === "arp"
        ? '<span class="ok-text">ARP · 可信</span>'
        : "IP包 · 参考";
    const gw = d.is_gateway ? ' <span class="tag">疑似网关</span>' : "";
    // 最近出现：越小越新鲜；0~1 秒显示“刚刚”
    const ago =
      d.seconds_ago == null
        ? "-"
        : d.seconds_ago <= 1
        ? "刚刚"
        : `${d.seconds_ago}s 前`;
    tr.innerHTML = `
      <td>${d.mac}</td>
      <td>${d.ip}${gw}</td>
      <td>${via}</td>
      <td>${ago}</td>
      <td><button class="btn" title="把这一行的 IP 填进第 ② 步的「对方当前 IP」，作为要连接、改动的目标。" onclick="pick('${d.ip}')">选为目标</button></td>`;
    tb.appendChild(tr);
  }
}

// 停止扫描：停掉前端轮询 + 通知后端停掉后台抓包。
async function stopScan() {
  stopScanTimer();
  document.getElementById("stopScanBtn").classList.add("hidden");
  await post("/api/scan_stop", {});
  const n = document.querySelectorAll("#devTable tbody tr").length;
  document.getElementById("scanStatus").textContent = `已停止扫描。共发现 ${n} 台设备。`;
}

function stopScanTimer() {
  if (scanTimer) {
    clearInterval(scanTimer);
    scanTimer = null;
  }
}

// 关闭 / 刷新页面时，尽量通知后端把后台抓包也停掉（best-effort）。
window.addEventListener("beforeunload", () => {
  if (scanTimer) navigator.sendBeacon("/api/scan_stop");
});

// 把发现的对方 IP 填进第 ② 步的「对方当前 IP」框
function pick(ip) {
  document.querySelector("[name=peer_ip]").value = ip;
}

// ===== ② 预览：只向后端要「测试连接时将执行的只读命令文本」，本身不登录对方、不发送账号密码 =====
// 和第③步的 previewChange 一致：必填项（对方当前 IP / 账号 / 密码）都填好后才允许预览。
async function previewTest() {
  const box = document.getElementById("testPreviewBox");
  box.classList.remove("hidden");
  if (!collectConn()) {
    box.textContent = "请先填好「对方当前 IP」「账号」「密码」，再预览。";
    return;
  }
  box.textContent = "正在生成预览……";
  const r = await post("/api/ssh_test", { preview: true });
  box.textContent = r.ok ? r.script : "无法生成预览：" + r.error;
}

// ===== ② 测试连接：登录对方并回显只读诊断信息 =====
// 说明：本机和对方不同网段时，后端会自动临时加同网段 IP 连过去、读完删掉，前端不用管。
async function sshTest() {
  const out = document.getElementById("testOutput");
  const conn = collectConn();
  if (!conn) {
    out.innerHTML = '<span class="err">请先填好「对方当前 IP」「账号」「密码」。</span>';
    return;
  }
  out.innerHTML = "正在登录对方并读取信息……";
  const r = await post("/api/ssh_test", conn);
  if (!r.ok) {
    out.innerHTML = `<span class="err">${escapeHtml(r.error)}</span>`;
    return;
  }
  let html = '<div class="rc ok">登录成功</div>';
  html += tempNote(r.temp_used);
  // 从回显里认出「IP 正好等于对方当前 IP」的那个网口，自动填进第 ③ 步的「对方网口名」
  html += autofillRemoteIface(r.stdout || "", conn.peer_ip);
  html += `<pre class="std">${escapeHtml(r.stdout || "（无输出）")}</pre>`;
  if (r.stderr) html += `<pre class="stderr">${escapeHtml(r.stderr)}</pre>`;
  out.innerHTML = html;
}

// 从「测试连接」回显的 `ip -br addr` 段里，找出 IP 正好等于对方当前 IP 的那个网口，
// 自动填进第 ③ 步的「对方网口名」。返回一行提示 html；没找到、或用户已手填则返回空串、不覆盖。
function autofillRemoteIface(stdout, peerIp) {
  const box = document.querySelector("[name=remote_iface]");
  if (!box || !peerIp || !stdout) return "";
  if (box.value.trim()) return ""; // 已手动填过就不覆盖
  let inAddrSection = false;
  for (const line of stdout.split("\n")) {
    // 回显分成多段（主机名 / 网口和 IP / 路由 / netplan…）。只在「ip -br addr」那段里找，
    // 免得把路由行、netplan yaml 里出现的同一个 IP 误当成网口名。
    if (line.includes("=====")) {
      inAddrSection = line.includes("ip -br addr");
      continue;
    }
    if (!inAddrSection) continue;
    // ip -br addr 每行形如：eth0  UP  192.168.5.10/24 fe80::.../64
    const cols = line.trim().split(/\s+/);
    if (cols.length < 3 || cols[0] === "lo") continue;
    const hit = cols.slice(2).some((a) => a.split("/")[0] === peerIp);
    if (hit) {
      box.value = cols[0];
      return `<div class="hint">（已自动把对方网口名「${escapeHtml(cols[0])}」填进第 ③ 步；如与预期不符可手动改。）</div>`;
    }
  }
  return "";
}

// 若后端为了连通对方自动加过临时源地址 + 主机路由（用完已删），给一行说明。
function tempNote(temp) {
  if (!temp) return "";
  return `<div class="hint">（为连到对方：已自动临时给 ${temp.iface} 加源地址 ${temp.local_ip}、并把到 ${temp.peer_ip} 的路由钉在该口，操作完成后已自动移除。）</div>`;
}

// ===== ③ 修改对方 IP =====

// 读 ③ 表单里的四个参数（对方网口 / 新 IP / 前缀 / 网关）。
function changeParams() {
  return {
    remote_iface: getVal("remote_iface"),
    new_ip: getVal("new_ip"),
    new_prefix: getVal("new_prefix") || "24",
    gateway: getVal("gateway"),
  };
}

// 预览：只向后端要「将执行的命令文本」，不连对方、不改任何东西。
// 必填项（对方网口名 / 新 IP）都填好后才允许预览，和「执行」保持一致。
async function previewChange() {
  const box = document.getElementById("previewBox");
  const p = changeParams();
  box.classList.remove("hidden");
  if (!p.remote_iface || !p.new_ip) {
    box.textContent = "请先填好「对方网口名」和「新 IP」，再预览。";
    return;
  }
  box.textContent = "正在生成预览……";
  const r = await post("/api/change_ip", { ...p, preview: true });
  box.textContent = r.ok ? r.script : "无法生成预览：" + r.error;
}

// 执行：用和预览完全相同的那段命令，通过 SSH 跑 备份→写入→应用。
async function runChange() {
  const p = changeParams();
  const out = document.getElementById("changeOutput");
  if (!p.remote_iface || !p.new_ip) {
    out.innerHTML = '<span class="err">请先填「对方网口名」和「新 IP」。</span>';
    return;
  }
  const conn = collectConn();
  if (!conn) {
    out.innerHTML = '<span class="err">请先在第 ② 步填好「对方当前 IP」「账号」「密码」。</span>';
    return;
  }
  const ok = confirm(
    `确认把 ${p.remote_iface} 改成 ${p.new_ip}/${p.new_prefix} 吗？\n` +
      `程序会全自动完成：必要时临时打通网络 → 备份 → 写入 → 应用 → 清理临时 IP → 验证新 IP。\n` +
      `应用瞬间旧连接会断开，属正常。`
  );
  if (!ok) return;

  out.innerHTML = "正在全自动执行：临时打通 → 备份 → 写入 → 应用 → 清理 → 验证新 IP……";
  const r = await post("/api/change_ip", { ...conn, ...p });
  renderChangeResult(out, r);
}

// 渲染「改 IP」结果：把「新 IP 通不通」这个最重要的结论放最上面、最醒目。
function renderChangeResult(out, r) {
  if (!r.ok) {
    out.innerHTML = `<span class="err">错误：${escapeHtml(r.error)}</span>`;
    return;
  }
  let html = "";
  if (r.new_reachable === true) {
    html += `<div class="rc ok">新 IP 已生效：${escapeHtml(r.verify_note || "")}</div>`;
  } else if (r.new_reachable === false) {
    html += `<div class="rc bad">${escapeHtml(r.verify_note || "新 IP 验证不通过。")}</div>`;
  } else {
    html += `<div class="rc">${escapeHtml(r.verify_note || "已提交修改，但没有自动验证新 IP。")}</div>`;
  }
  html += tempNote(r.temp_used);
  // 对方已搬到新 IP：确认 ping 通了，就把「对方当前 IP」自动更新为新 IP，
  // 这样之后「测试连接」「还原到备份」连的都是对方的真实位置（而不是失效的旧 IP）。
  if (r.new_reachable === true && r.new_ip) {
    const box = document.querySelector("[name=peer_ip]");
    if (box) {
      box.value = r.new_ip;
      html += `<div class="hint">（对方已在新 IP 上，已把「对方当前 IP」更新为 ${escapeHtml(r.new_ip)}；之后「测试连接」「还原到备份」都会连它。）</div>`;
    }
  } else if (r.new_reachable === false && r.new_ip) {
    html += `<div class="hint">（若确认对方已搬到 ${escapeHtml(r.new_ip)}，可手动把「对方当前 IP」改成它再重试；若彻底连不上，只能物理接触对方恢复。）</div>`;
  }
  // 远端执行命令的原始输出（备份/写入/应用的回显），折叠在下面供参考
  if (r.stdout) html += `<pre class="std">${escapeHtml(r.stdout)}</pre>`;
  if (r.stderr) html += `<pre class="stderr">${escapeHtml(r.stderr)}</pre>`;
  out.innerHTML = html;
}

// 还原：把之前备份的 netplan 配置拷回去并重新应用。
async function restoreIp() {
  const out = document.getElementById("changeOutput");
  const conn = collectConn();
  if (!conn) {
    out.innerHTML = '<span class="err">请先在第 ② 步填好「对方当前 IP」「账号」「密码」。</span>';
    return;
  }
  if (!confirm("确认还原到之前备份的 netplan 配置吗？")) return;
  out.innerHTML = "正在还原备份……";
  const r = await post("/api/restore_ip", conn);
  renderExec(out, r);
}

// 页面一打开就加载网口列表
loadIfaces();
