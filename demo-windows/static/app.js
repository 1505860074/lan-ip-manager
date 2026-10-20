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

// ===== ① 网口列表 =====

// 把网口名转成只含字母数字的安全串，用来拼 DOM 的 id（网口名一般已是字母数字，
// 但保险起见把别的字符换成下划线，避免拼出非法/歧义的选择器）。
function safeId(name) {
  return String(name).replace(/[^A-Za-z0-9]/g, "_");
}

// 来源标签：把后端给的 kind 显示成中文小标签。
function kindTag(kind) {
  if (kind === "permanent") return '<span class="ipkind perm">永久</span>';
  if (kind === "dhcp") return '<span class="ipkind dhcp">DHCP</span>';
  return '<span class="ipkind temp">临时</span>'; // manual
}

async function loadIfaces() {
  const r = await (await fetch("/api/interfaces")).json();
  // 顶部 root 警告：不是 root 才显示
  document.getElementById("rootWarn").classList.toggle("hidden", r.root);

  const tb = document.querySelector("#ifaceTable tbody");
  tb.innerHTML = "";
  if (!r.ok) {
    tb.innerHTML = `<tr><td colspan="4">错误：${r.error}</td></tr>`;
    return;
  }
  _lastIfaces = r.interfaces; // 缓存，供「临时改主 IP」取旧地址用
  for (const i of r.interfaces) {
    const sid = safeId(i.name);

    // 「当前 IP」列：只逐条显示地址 + 来源标签（无 IP 时显示 -）。
    // 删除按钮不放这里——常显列里太容易误点删掉地址；删除移到「配置本机 IP」面板内。
    let ipCell = "-";
    const det = i.ip_detail || [];
    if (det.length) {
      ipCell = det
        .map((d) => `<div class="ipitem">${d.cidr} ${kindTag(d.kind)}</div>`)
        .join("");
    }

    // 网口主行：整行可点击选中；未选中时不显示任何操作按钮。
    const tr = document.createElement("tr");
    tr.className = "iface-row";
    if (i.carrier) tr.classList.add("connected"); // 已连网口整行淡蓝高亮，一眼看到
    tr.dataset.iface = i.name;
    tr.dataset.sid = sid;
    tr.onclick = () => selectIface(i.name);
    tr.innerHTML = `
      <td><span class="sel-mark">▶</span>${i.name}</td>
      <td>${i.carrier ? "<b>已连</b>" : "未连"}</td>
      <td>${i.operstate}</td>
      <td>${ipCell}</td>`;
    tb.appendChild(tr);

    // 紧跟一行「详情行」：默认随行收起，选中该行才展开。
    // 里面放操作按钮（扫描找设备 / 配置本机 IP）+ 配置面板，横跨整表宽度，不再挤在窄列里。
    const scanBtn = i.carrier
      ? `<button class="btn" title="在这个网口上监听，找出直连的对方设备；同时把它自动填进第 ② 步的「本机直连网口」。" onclick="scan('${i.name}')">扫描找设备</button>`
      : `<button class="btn" disabled title="该口未插网线（未连），无法扫描。">扫描找设备</button>`;
    const cfgBtn = `<button class="btn" title="给这个网口新增 IP / 改主 IP（临时或永久）。" onclick="toggleCfg('${i.name}')">配置本机 IP</button>`;

    // 配置面板默认收起；若这个网口的面板在重绘前是打开的，则保持打开（删完一个 IP
    // 会重绘整张表，不保持的话面板会跟着收起，连删多个很别扭）。
    const cfgHidden = openCfgIfaces.has(i.name) ? "" : " hidden";
    const detail = document.createElement("tr");
    detail.className = "detail-row hidden";
    detail.id = `detail-${sid}`;
    detail.innerHTML =
      `<td colspan="4">` +
      `<div class="iface-actions">${scanBtn}${cfgBtn}</div>` +
      `<div id="cfg-${sid}" class="cfg-panel${cfgHidden}">${cfgPanelHtml(i.name, sid, det)}</div>` +
      `</td>`;
    tb.appendChild(detail);
  }

  // 保证有且仅有一个网口被选中：沿用上次选中的口；若它已不在（或首次加载）就选第一个。
  const names = r.interfaces.map((i) => i.name);
  if (!names.includes(selectedIface)) selectedIface = names[0] || null;
  applySelection();
}

// 当前被选中的网口名（全局，跨 loadIfaces 重绘保持）。
let selectedIface = null;

// 选中某个网口：更新全局状态并重新套用高亮/展开。
function selectIface(name) {
  selectedIface = name;
  applySelection();
}

// 按 selectedIface 给对应行加 .selected 高亮、展开它的详情行，其余收起。
function applySelection() {
  const rows = document.querySelectorAll("#ifaceTable tbody tr.iface-row");
  rows.forEach((row) => {
    const isSel = row.dataset.iface === selectedIface;
    row.classList.toggle("selected", isSel);
    const detail = document.getElementById(`detail-${row.dataset.sid}`);
    if (!detail) return;
    detail.classList.toggle("hidden", !isSel);
    // 只对当前展开的那行算字号：隐藏的行宽度为 0，量不出来。
    if (isSel) fitActionBar(detail.querySelector(".iface-actions"));
  });
}

// 让一排操作按钮在等分宽度后仍放得下文字：从基准字号逐级往下试，
// 一旦所有按钮都不再溢出（文字放得下）就停在那个字号。按钮多、列窄时自动变小。
function fitActionBar(bar) {
  if (!bar) return;
  const btns = bar.querySelectorAll(".btn");
  if (!btns.length) return;
  const MAX = 18, MIN = 11; // 字号上下限（px）
  for (let size = MAX; size >= MIN; size--) {
    bar.style.fontSize = size + "px";
    // scrollWidth > clientWidth 说明这个按钮里的文字比它被分到的宽度还宽 = 放不下
    let overflow = false;
    for (const b of btns) {
      if (b.scrollWidth > b.clientWidth + 1) { overflow = true; break; }
    }
    if (!overflow) break; // 这个字号下都放得下，就用它
  }
}

// 给所有「当前可见」的按钮条重算字号：②③ 步的 .term-actions 一直在，
// ① 里的 .iface-actions 只有选中那行是展开可见的（隐藏的量不出宽度）。
function fitAllBars() {
  document.querySelectorAll(".term-actions").forEach(fitActionBar);
  const sel = document.querySelector("#ifaceTable tbody tr.iface-row.selected");
  if (sel) {
    const detail = document.getElementById(`detail-${sel.dataset.sid}`);
    if (detail) fitActionBar(detail.querySelector(".iface-actions"));
  }
}

// 窗口尺寸变化会改变每列宽度，需要重算一次所有可见按钮条的字号。
window.addEventListener("resize", fitAllBars);

// 生成某网口配置面板的 HTML。radio 用 name="mode-<sid>" 保证各网口互不串。
// det = 该网口当前的地址列表（i.ip_detail）：在面板顶部逐条列出，每条后带删除按钮。
function cfgPanelHtml(name, sid, det) {
  det = det || [];
  // 现有 IP 列表：每条地址 + 来源标签 + 删除按钮（删除挪到这里，避免在常显列误点）。
  let ipList = '<div class="cfg-ip-empty dim">（该网口暂无 IP）</div>';
  if (det.length) {
    ipList = det
      .map((d) => {
        const perm = d.kind === "permanent";
        const title = perm
          ? "从 netplan 删除这个永久地址（重启保留）"
          : "临时删除这个运行态地址（等同 ip addr del）";
        const delBtn =
          `<button class="btn danger ipdel" title="${title}" ` +
          `onclick="delLocalIp('${name}','${d.ip}',${d.prefix},${perm})">删除</button>`;
        return `<div class="cfg-ip-row">` +
          `<span class="cfg-ip-addr">${d.cidr}</span> ${kindTag(d.kind)} ${delBtn}` +
          `</div>`;
      })
      .join("");
  }
  return `
    <div class="local-cfg">
      <div class="cfg-iplist">
        <div class="cfg-iplist-title">本机网口现有 IP（点「删除」移除对应地址）：</div>
        ${ipList}
      </div>
      <div class="cfg-line">
        <span>生效时长：</span>
        <label class="inline"><input type="radio" name="mode-${sid}" value="temp" checked onchange="onModeChange('${sid}')"> 临时（ip addr，重启失效）</label>
        <label class="inline"><input type="radio" name="mode-${sid}" value="perm" onchange="onModeChange('${sid}')"> 永久（写 netplan，重启保留）</label>
      </div>
      <div class="cfg-line">
        <label class="inline">IP <input class="cfg-ip" placeholder="如 192.168.10.5"></label>
        <label class="inline">前缀 <input class="cfg-prefix" type="number" value="24" style="width:5em"></label>
      </div>
      <div class="cfg-line cfg-perm-only hidden">
        <label class="inline">网关 <input class="cfg-gw" placeholder="留空=不配"></label>
        <label class="inline">DNS <input class="cfg-dns" placeholder="逗号分隔，可留空"></label>
        <small class="hint">（网关 / DNS 仅在「永久 · 改主 IP」时生效）</small>
      </div>
      <div class="cfg-line">
        <button class="btn" title="在该网口上叠加一个新 IP（别名），不动原有 IP。" onclick="addLocalIp('${name}','${sid}')">新增此 IP（别名）</button>
        <button class="btn danger" title="用这个 IP 替换该网口的主 IP；会先弹窗二次确认。" onclick="changeMainIp('${name}','${sid}')">设为主 IP（替换）</button>
      </div>
    </div>`;
}

// 记录哪些网口的配置面板当前是展开的，好在重绘整张表后仍保持展开。
let openCfgIfaces = new Set();

// 切换某网口配置面板的展开/收起
function toggleCfg(name) {
  const row = document.getElementById(`cfg-${safeId(name)}`);
  if (!row) return;
  const nowHidden = row.classList.toggle("hidden");
  if (nowHidden) openCfgIfaces.delete(name);
  else openCfgIfaces.add(name);
}

// 切「临时/永久」时：只有「永久」才显示网关/DNS 那一行。
function onModeChange(sid) {
  const perm = isPerm(sid);
  const line = document.querySelector(`#cfg-${sid} .cfg-perm-only`);
  if (line) line.classList.toggle("hidden", !perm);
}

// 读某网口面板里选的是不是「永久」
function isPerm(sid) {
  const el = document.querySelector(`#cfg-${sid} input[name="mode-${sid}"]:checked`);
  return el ? el.value === "perm" : false;
}

// 读某网口面板里的一个输入框（cls 如 "cfg-ip"）
function cfgVal(sid, cls) {
  const el = document.querySelector(`#cfg-${sid} .${cls}`);
  return el ? el.value.trim() : "";
}

// 统一显示本机配置操作的结果（成功/失败），并刷新网口列表。
function showLocalResult(r) {
  const box = document.getElementById("localResult");
  if (r.ok) {
    box.innerHTML = `<div class="rc ok">${escapeHtml(r.message || "操作完成。")}</div>`;
  } else {
    box.innerHTML = `<div class="rc bad">错误：${escapeHtml(r.error || "未知错误")}</div>`;
  }
  loadIfaces(); // 重新拉一遍，最新的 IP 列表立刻反映出来
}

// 新增别名 IP（临时/永久由面板 radio 决定）。不弹二次确认。
async function addLocalIp(name, sid) {
  const ip = cfgVal(sid, "cfg-ip");
  const prefix = cfgVal(sid, "cfg-prefix") || "24";
  if (!ip) {
    showLocalResult({ ok: false, error: "请先填写要新增的 IP。" });
    return;
  }
  const r = await post("/api/local/add_ip", {
    iface: name, ip, prefix, persist: isPerm(sid),
  });
  showLocalResult(r);
}

// 改主 IP（替换）。临时/永久都要二次确认。
async function changeMainIp(name, sid) {
  const ip = cfgVal(sid, "cfg-ip");
  const prefix = cfgVal(sid, "cfg-prefix") || "24";
  if (!ip) {
    showLocalResult({ ok: false, error: "请先填写新的主 IP。" });
    return;
  }
  const perm = isPerm(sid);
  const how = perm ? "永久（写 netplan，重启保留）" : "临时（重启失效）";
  if (!confirm(
    `确认把 ${name} 的主 IP 替换成 ${ip}/${prefix} 吗？（${how}）\n` +
    "替换会瞬间断开基于旧 IP 的连接；若你正通过该网口远程访问本机，可能会断线。\n" +
    "（本工具的页面走 127.0.0.1，通常不受影响。）"
  )) return;

  // 临时替换需要知道要删掉的旧主 IP：取该网口当前第一个地址（若取不到就只加不删）。
  let old_ip = "", old_prefix = "";
  if (!perm) {
    const first = firstAddrOf(name);
    if (first) { old_ip = first.ip; old_prefix = first.prefix; }
  }
  const r = await post("/api/local/change_main", {
    iface: name, new_ip: ip, new_prefix: prefix, persist: perm,
    gateway: cfgVal(sid, "cfg-gw"), dns: cfgVal(sid, "cfg-dns"),
    old_ip, old_prefix,
  });
  showLocalResult(r);
}

// 从当前已渲染的网口行里，取某网口的第一个 IPv4 地址（用于临时改主 IP 时删旧地址）。
// 直接从缓存的最近一次接口数据取，避免再发一次请求。
let _lastIfaces = [];
function firstAddrOf(name) {
  const i = _lastIfaces.find((x) => x.name === name);
  if (i && i.ip_detail && i.ip_detail.length) {
    return { ip: i.ip_detail[0].ip, prefix: i.ip_detail[0].prefix };
  }
  return null;
}

// 删除某个地址：永久地址从 netplan 删，其余按临时删（ip addr del）。不弹二次确认。
async function delLocalIp(name, ip, prefix, permanent) {
  const r = await post("/api/local/del_ip", {
    iface: name, ip, prefix, permanent: !!permanent,
  });
  showLocalResult(r);
}

// 把某网口名填进第 ② 步的「本机直连网口」框
function useIface(name) {
  document.querySelector("[name=iface]").value = name;
}

// ===== ① 持续扫描：后台一直抓包，前端每秒刷新一次结果 =====
let scanTimer = null; // setInterval 句柄
let scanStartMs = 0; // 本轮开始时刻，用于 3 分钟安全上限
const SCAN_MAX_MS = 180000; // 连续扫描最多 3 分钟，避免忘了停

// ---- 访问日志实时回显：像 tail -f 一样，定时把 logs/access.log 的新增内容贴进 #scanLog ----
// 是不是卡住，看上面每秒刷新的 #scanStatus 就够了；这个窗口直接显示后端 access.log
// 的原始内容（每次请求都会写一行，扫描时每秒刷新，能看到实时在动）。
let logTimer = null; // 拉日志的 setInterval 句柄
let logOffset = 0; // 已读到 access.log 的字节位置，下次带上只取增量
const LOG_POLL_MS = 1500;

// 追加一行到窗口；含 ERROR 的行标红。自动滚到底。
function appendLogLine(text) {
  const box = document.getElementById("scanLog");
  if (!box) return;
  const line = document.createElement("div");
  line.className = "logline" + (/\bERROR\b/.test(text) ? " err" : "");
  line.textContent = text;
  box.appendChild(line);
  while (box.childElementCount > 500) box.removeChild(box.firstChild); // 防 DOM 堆爆
  box.scrollTop = box.scrollHeight; // 始终滚到最新一行
}

// 拉取 access.log 从 logOffset 起的新增内容并逐行追加（页面打开即开始，一直跟）。
async function pollLogs() {
  let r;
  try {
    r = await (await fetch(`/api/logs?offset=${logOffset}`)).json();
  } catch (e) {
    return; // 偶发抖动，忽略这一拍
  }
  if (!r.ok) return;
  logOffset = r.offset; // 无论有没有新内容都推进 offset
  if (!r.text) return;
  const box = document.getElementById("scanLog");
  if (box) box.querySelectorAll(".logline.dim").forEach((el) => el.remove()); // 清占位提示
  for (const ln of r.text.split("\n")) {
    if (ln.length) appendLogLine(ln);
  }
}

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
  // 这样之后「测试连接」「显示备份文件」连的都是对方的真实位置（而不是失效的旧 IP）。
  if (r.new_reachable === true && r.new_ip) {
    const box = document.querySelector("[name=peer_ip]");
    if (box) {
      box.value = r.new_ip;
      html += `<div class="hint">（对方已在新 IP 上，已把「对方当前 IP」更新为 ${escapeHtml(r.new_ip)}；之后「测试连接」「显示备份文件」都会连它。）</div>`;
    }
  } else if (r.new_reachable === false && r.new_ip) {
    html += `<div class="hint">（若确认对方已搬到 ${escapeHtml(r.new_ip)}，可手动把「对方当前 IP」改成它再重试；若彻底连不上，只能物理接触对方恢复。）</div>`;
  }
  // 远端执行命令的原始输出（备份/写入/应用的回显），折叠在下面供参考
  if (r.stdout) html += `<pre class="std">${escapeHtml(r.stdout)}</pre>`;
  if (r.stderr) html += `<pre class="stderr">${escapeHtml(r.stderr)}</pre>`;
  out.innerHTML = html;
}

// 显示备份文件：只读地读取并显示对方 /tmp/netplan-bak/ 里的原始 netplan 配置，
// 同时后端会把完整内容写进本机日志。不改动对方任何东西。
async function showBackup() {
  const out = document.getElementById("changeOutput");
  const conn = collectConn();
  if (!conn) {
    out.innerHTML = '<span class="err">请先在第 ② 步填好「对方当前 IP」「账号」「密码」。</span>';
    return;
  }
  out.innerHTML = "正在读取对方备份文件……";
  const r = await post("/api/show_backup", conn);
  if (!r.ok) {
    out.innerHTML = `<span class="err">${escapeHtml(r.error)}</span>`;
    return;
  }
  let html = '<div class="rc ok">备份文件原始内容（已同时完整写入本机日志 logs/app.log）</div>';
  html += tempNote(r.temp_used);
  html += `<pre class="std">${escapeHtml(r.stdout || "（无内容）")}</pre>`;
  if (r.stderr) html += `<pre class="stderr">${escapeHtml(r.stderr)}</pre>`;
  out.innerHTML = html;
}

// 退出程序：请求后端关闭自己。后端会先返回 ok，再延时半秒退出进程；
// 之后本页面的所有请求都会失败，所以这里退出后就把界面切成一个“已退出”提示页。
async function quitApp() {
  if (!confirm("确定要退出程序吗？退出后本页面将无法再操作，需要重新启动程序。")) return;
  try {
    await post("/api/quit", {});
  } catch (e) {
    // 后端可能在返回前就断了，忽略即可
  }
  // 停掉两个轮询定时器，免得它们不停地对已关闭的后端报错
  if (scanTimer) clearInterval(scanTimer);
  if (logTimer) clearInterval(logTimer);
  document.body.innerHTML =
    '<div style="max-width:640px;margin:80px auto;text-align:center;line-height:1.8">' +
    "<h1>程序已退出</h1>" +
    "<p>后端进程已关闭，本页面已失效。<br>如需继续使用，请重新启动程序。</p>" +
    "<p>可以直接关闭此浏览器标签页。</p></div>";
}

// 页面一打开就加载网口列表
loadIfaces();

// 页面一打开就开始跟运行日志：立刻拉一次，之后每 LOG_POLL_MS 拉增量。
pollLogs();
logTimer = setInterval(pollLogs, LOG_POLL_MS);

// 首屏给 ②③ 步的操作按钮条算一次字号（它们在静态 HTML 里，已就绪）。
fitAllBars();
