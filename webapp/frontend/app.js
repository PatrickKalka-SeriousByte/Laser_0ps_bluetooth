const responseBox = document.getElementById("response");
const notificationsBox = document.getElementById("notifications");
const statusPill = document.getElementById("status-pill");
const errorsBand = document.getElementById("errors-band");
const errorsBox = document.getElementById("errors");
const clearErrorsButton = document.getElementById("btn-clear-errors");
const targetAddressInput = document.getElementById("target-address");
const liveStatePill = document.getElementById("live-state");
const liveStatusBody = document.getElementById("live-status-body");
const liveEventsBox = document.getElementById("live-events");
const scanButton = document.getElementById("btn-scan");
const enableBluetoothButton = document.getElementById("btn-enable-bluetooth");
const setupBody = document.getElementById("setup-body");
const rankingBody = document.getElementById("ranking-body");
const rankingStatePill = document.getElementById("ranking-state");
const roundStatsBody = document.getElementById("round-stats-body");
const gameSummaryPill = document.getElementById("game-summary");
const languageSelect = document.getElementById("language-select");
const SLOT_MIN = 1;
const SLOT_MAX = 10;
const LEVEL_MIN = 1;
const LEVEL_MAX = 5;
const PROFILE_BYTE_MIN = 0;
const PROFILE_BYTE_MAX = 255;
if (enableBluetoothButton) {
  enableBluetoothButton.classList.add("hidden");
}

const SUPPORTED_LANGUAGES = ["en", "de"];
const DEFAULT_LANGUAGE = "en";
let currentLanguage = DEFAULT_LANGUAGE;
let translations = {
  "app.title": "LaserOps Control",
  "status.apiUnknown": "API: unknown",
  "status.liveOff": "Live stream: off",
  "error.enterTargetAddress": "Please enter a target address first.",
  "error.noTargetAddressSet": "No target address set.",
  "error.unknown": "Unknown error",
  "table.blasterType": "Blaster type",
  "table.blasterConfig": "Blaster Config",
  "table.level": "Level",
  "table.life": "Life",
  "table.damage": "Damage",
  "table.profile": "Profile (HP/D/A)",
  "table.unknownType": "Unknown",
  "blasterType.alphaPoint": "AlphaPoint",
  "blasterType.deltaBurst": "DeltaBurst",
  "button.saveLevel": "Save level",
  "button.saveConfig": "Save config",
  "button.resetConfig": "Reset config",
  "button.connecting": "Connecting...",
  "label.healthProfile": "Health (0-255)",
  "label.damageProfile": "Damage (0-255)",
  "label.ammoProfile": "Ammo (0-255)",
  "confirm.resetConfig": "Reset this blaster to default level/profile values?",
  "misc.empty": "-",
};

function detectLanguage() {
  const browserLang = String(navigator.language || "").toLowerCase();
  const short = browserLang.split("-")[0];
  return SUPPORTED_LANGUAGES.includes(short) ? short : DEFAULT_LANGUAGE;
}

function languageFileFor(lang) {
  return lang === "de" ? "german.json" : "english.json";
}

async function loadLanguage(lang) {
  const safeLang = SUPPORTED_LANGUAGES.includes(lang) ? lang : DEFAULT_LANGUAGE;
  const response = await fetch(`/assets/i18n/${languageFileFor(safeLang)}`);
  if (!response.ok) {
    throw new Error(`Failed to load translations for ${safeLang}`);
  }
  translations = await response.json();
  currentLanguage = safeLang;
  document.documentElement.lang = safeLang;
  if (languageSelect) {
    languageSelect.value = safeLang;
  }
}

function interpolate(template, vars = {}) {
  return String(template).replace(/\{(\w+)\}/g, (_, key) => String(vars[key] ?? ""));
}

function t(key, vars = {}) {
  const template = translations[key];
  if (typeof template !== "string") {
    return key;
  }
  return interpolate(template, vars);
}

function applyTranslations() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    el.setAttribute("placeholder", t(el.dataset.i18nPlaceholder));
  });
  document.title = t("app.title");
  setScanBusy(scanBusy);
  setConnectBusy(connectBusy);
  renderErrors();
  renderCachedDevices();
}

let liveSource = null;
const liveByAddress = new Map();
const scannedDevicesByAddress = new Map();
const liveLines = [];
const MAX_LIVE_LINES = 2000;
const LIVE_SEPARATOR = "----------------------------------------";
const errorEntries = [];
const MAX_ERROR_LINES = 100;
const errorLastSeenByFingerprint = new Map();
let scanBusy = false;
let scanBusyTimer = null;
let connectBusy = false;

let currentView = "setup";
let lastRankingSnapshot = {
  running: false,
  session_state: "idle",
  session_id: null,
  started_at: null,
  planned_end_at: null,
  duration_seconds: null,
  remaining_seconds: null,
  ended_at: null,
  end_reason: null,
  participants: [],
  ranking: [],
  slot_stats: [],
  totals: { shots: 0, reloads: 0, hits: null, kills: null, assists: null },
};

function normalizeDetail(raw) {
  if (raw == null) return "";
  if (typeof raw === "string") return raw.trim();
  if (Array.isArray(raw)) {
    return raw
      .map((item) => normalizeDetail(item))
      .filter((item) => item.length > 0)
      .join("; ");
  }
  if (typeof raw === "object") {
    const loc = Array.isArray(raw.loc)
      ? raw.loc.filter((part) => part !== "body").join(".")
      : "";
    const msg = typeof raw.msg === "string" ? raw.msg.trim() : "";
    if (loc && msg) return `${loc}: ${msg}`;
    if (msg) return msg;
    if ("detail" in raw) return normalizeDetail(raw.detail);
    if ("error" in raw) return normalizeDetail(raw.error);
    try {
      return JSON.stringify(raw);
    } catch (_err) {
      return String(raw);
    }
  }
  return String(raw).trim();
}

function stripTupleDetail(raw) {
  const text = normalizeDetail(raw);
  const tupleMatch = text.match(/^\(['"](.+?)['"],\s*<[^>]+>\)$/);
  return tupleMatch ? tupleMatch[1] : text;
}

function localizeErrorDetail(detail, status) {
  const clean = stripTupleDetail(detail);
  const lower = clean.toLowerCase();

  if (
    lower.includes("no powered bluetooth adapters") ||
    lower.includes("powered_off")
  ) {
    return t("error.bluetoothPoweredOff");
  }
  if (lower.includes("failed to enable bluetooth")) {
    return t("error.bluetoothEnableFailed");
  }
  if (lower.includes("only supported on linux hosts")) {
    return t("error.bluetoothUnsupportedHost");
  }
  if (lower.includes("no supported bluetooth control utility found")) {
    return t("error.bluetoothToolsMissing");
  }
  if (
    lower.includes("bluetooth discovery already running") ||
    lower.includes("scan already in progress")
  ) {
    return t("error.scanAlreadyRunning");
  }
  if (lower.includes("not connected")) {
    return t("error.deviceNotConnected");
  }
  if (lower.includes("not found")) {
    return t("error.deviceNotFound");
  }
  if (lower.includes("timeout")) {
    return t("error.timeout");
  }
  if (lower.includes("permission denied") || lower.includes("access denied")) {
    return t("error.permissionDenied");
  }
  if (
    lower.includes("unsupported multiplayer team mix") ||
    lower.includes("mixed violet with red/blue") ||
    (lower.includes("teams 0/1") && lower.includes("all violet"))
  ) {
    return t("error.multiplayerTeamModeUnsupported");
  }
  if (lower.includes("failed to fetch")) {
    return t("error.network");
  }
  if (Number.isFinite(status) && status === 422) {
    return t("error.validationFailed", { status });
  }
  if (Number.isFinite(status) && status >= 500) {
    return t("error.serverFailure", { status });
  }
  if (Number.isFinite(status) && status >= 400) {
    return t("error.requestFailed", { status });
  }
  return clean || t("error.unknown");
}

function isScanBusyError(err) {
  const detail = stripTupleDetail(
    err?.detail ?? err?.message ?? (typeof err === "string" ? err : "")
  ).toLowerCase();
  return (
    detail.includes("bluetooth discovery already running") ||
    detail.includes("scan already in progress")
  );
}

function renderErrors() {
  if (!errorsBand || !errorsBox) return;
  if (errorEntries.length === 0) {
    errorsBox.textContent = "";
    errorsBand.classList.add("hidden");
    return;
  }
  errorsBand.classList.remove("hidden");
  const lines = errorEntries.map((entry) => {
    const context = t(entry.contextKey || "context.action");
    const summary = localizeErrorDetail(entry.detail, entry.status);
    const ts = new Date(entry.ts).toLocaleTimeString(currentLanguage);
    const header = `[${ts}] ${t("error.actionFailed", { context, message: summary })}`;
    if (entry.detail && stripTupleDetail(entry.detail) !== summary) {
      return `${header}\n${t("error.details")}: ${stripTupleDetail(entry.detail)}`;
    }
    return header;
  });
  errorsBox.textContent = lines.join("\n\n");
}

function clearErrors() {
  errorEntries.length = 0;
  errorLastSeenByFingerprint.clear();
  renderErrors();
}

function reportError(err, options = {}) {
  const status = Number(err?.status);
  const contextKey = options.contextKey || "context.action";
  const detail = stripTupleDetail(
    err?.detail ?? err?.message ?? (typeof err === "string" ? err : "")
  );
  const fingerprint = `${contextKey}|${Number.isFinite(status) ? status : ""}|${detail}`;
  const now = Date.now();
  const dedupeMs = Number(options.dedupeMs ?? 3000);
  const lastSeen = errorLastSeenByFingerprint.get(fingerprint) || 0;
  if (now - lastSeen < dedupeMs) {
    return;
  }
  errorLastSeenByFingerprint.set(fingerprint, now);
  errorEntries.push({
    ts: now,
    status: Number.isFinite(status) ? status : null,
    detail,
    contextKey,
  });
  if (errorEntries.length > MAX_ERROR_LINES) {
    errorEntries.splice(0, errorEntries.length - MAX_ERROR_LINES);
  }
  renderErrors();
}

function errorPayload(err, contextKey = "context.action") {
  const status = Number(err?.status);
  const detail = stripTupleDetail(
    err?.detail ?? err?.message ?? (typeof err === "string" ? err : "")
  );
  return {
    context: t(contextKey),
    status: Number.isFinite(status) ? status : null,
    error: localizeErrorDetail(detail, status),
    detail: detail || null,
  };
}

function setResponse(payload) {
  responseBox.textContent = JSON.stringify(payload, null, 2);
}

function setStatus(text, ok = true) {
  statusPill.textContent = text;
  statusPill.classList.toggle("error", !ok);
}

function setScanBusy(isBusy, retryMs = 0) {
  scanBusy = Boolean(isBusy);
  if (scanBusyTimer) {
    clearTimeout(scanBusyTimer);
    scanBusyTimer = null;
  }
  if (scanButton) {
    scanButton.disabled = scanBusy;
    scanButton.textContent = scanBusy ? t("button.scanning") : t("button.scan");
  }
  if (scanBusy && retryMs > 0) {
    scanBusyTimer = setTimeout(() => {
      setScanBusy(false);
    }, retryMs);
  }
}

function setConnectBusy(isBusy) {
  connectBusy = Boolean(isBusy);
  const body = document.getElementById("devices-body");
  if (!body) return;
  for (const button of body.querySelectorAll("button[data-address]")) {
    button.disabled = connectBusy;
    button.textContent = connectBusy ? t("button.connecting") : t("button.connect");
  }
}

function setLiveState(text, ok = true) {
  liveStatePill.textContent = text;
  liveStatePill.classList.toggle("error", !ok);
}

function setRankingState(text, ok = true) {
  rankingStatePill.textContent = text;
  rankingStatePill.classList.toggle("error", !ok);
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch (networkErr) {
    const err = new Error(networkErr?.message || "failed to fetch");
    err.status = 0;
    err.detail = networkErr?.message || "failed to fetch";
    err.path = path;
    throw err;
  }

  const contentType = String(response.headers.get("content-type") || "").toLowerCase();
  let data = null;
  let rawText = "";
  if (contentType.includes("application/json")) {
    data = await response.json().catch(() => null);
  } else {
    rawText = await response.text().catch(() => "");
  }

  if (!response.ok) {
    const candidateDetail = data?.detail ?? data?.error ?? rawText ?? response.statusText;
    const detail = stripTupleDetail(candidateDetail) || response.statusText || t("error.unknown");
    const err = new Error(detail);
    err.status = response.status;
    err.detail = detail;
    err.path = path;
    throw err;
  }
  return data ?? {};
}

function getTargetAddress() {
  return targetAddressInput.value.trim();
}

function isTargetConnected(address) {
  const key = normAddress(address);
  if (!key) return false;
  const entry = liveByAddress.get(key);
  if (!entry) return false;
  return String(entry.connection_state || "").toLowerCase() === "connected";
}

function clearTargetIfMatches(address) {
  const current = getTargetAddress();
  if (!current) return;
  if (normAddress(current) !== normAddress(address)) return;
  targetAddressInput.value = "";
  notificationsBox.textContent = t("error.noTargetAddressSet");
}

function mustTargetAddress() {
  const address = getTargetAddress();
  if (!address) {
    throw new Error(t("error.enterTargetAddress"));
  }
  return address;
}

function tsToLocal(ts) {
  if (!ts) return t("misc.empty");
  return new Date(ts * 1000).toLocaleString(currentLanguage);
}

function tsToClock(ts) {
  if (!ts) return t("misc.empty");
  return new Date(ts * 1000).toLocaleTimeString(currentLanguage);
}

function clampInt(value, min, max, fallback = min) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, Math.trunc(n)));
}

function safeSlotValue(value) {
  return clampInt(value, SLOT_MIN, SLOT_MAX, SLOT_MIN);
}

function deriveLevelValue(conn) {
  const fromSnapshot = Number(conn?.last_snapshot?.level);
  const fromLive = Number(conn?.live_state?.startup_level);
  if (Number.isFinite(fromSnapshot)) {
    return clampInt(fromSnapshot, LEVEL_MIN, LEVEL_MAX, LEVEL_MIN);
  }
  if (Number.isFinite(fromLive)) {
    return clampInt(fromLive, LEVEL_MIN, LEVEL_MAX, LEVEL_MIN);
  }
  return LEVEL_MIN;
}

function deriveConfigNameIndices(conn) {
  const snapshot = conn?.last_snapshot || {};
  const liveState = conn?.live_state || {};
  const rawNameA = Number(snapshot.name_a);
  const rawNameB = Number(snapshot.name_b);
  const fallbackRawNameA = Number(liveState.startup_name_a);
  const fallbackRawNameB = Number(liveState.startup_name_b);
  const resolvedRawNameA = Number.isFinite(rawNameA) ? rawNameA : fallbackRawNameA;
  const resolvedRawNameB = Number.isFinite(rawNameB) ? rawNameB : fallbackRawNameB;
  return {
    nameAIndex: clampInt(resolvedRawNameA - 1, 0, 49, 0),
    nameBIndex: clampInt(resolvedRawNameB - 1, 0, 49, 0),
  };
}

function extractProfileFromStartupRaw(rawHex) {
  if (typeof rawHex !== "string") return null;
  const clean = rawHex.trim().toLowerCase();
  if (!/^[0-9a-f]+$/.test(clean) || clean.length < 26) return null;
  const payload = clean.slice(0, 26);
  const parseByte = (index) => {
    const offset = index * 2;
    const pair = payload.slice(offset, offset + 2);
    if (pair.length !== 2) return null;
    const value = Number.parseInt(pair, 16);
    return Number.isFinite(value) ? value : null;
  };
  const ammo = parseByte(2);
  const damage = parseByte(3);
  const health = parseByte(7);
  if (ammo == null && damage == null && health == null) return null;
  return {
    ammo_profile: ammo,
    damage_profile: damage,
    health_profile: health,
  };
}

function defaultProfileByBlasterType(conn) {
  const configProfile = getConfigProfileForEntry(conn);
  const configByte7 = Number(configProfile?.byte7);
  if (Number.isFinite(configByte7)) {
    if (configByte7 === 1) {
      return {
        ammo_profile: 0x12,
        damage_profile: 0x01,
        health_profile: 0x0a,
      };
    }
    if (configByte7 === 0) {
      return {
        ammo_profile: 0x0a,
        damage_profile: 0x02,
        health_profile: 0x0a,
      };
    }
  }
  const type = String(
    conn?.blaster_type ||
      conn?.last_snapshot?.blaster_type ||
      conn?.live_state?.startup_blaster_type ||
      ""
  ).toLowerCase();
  if (type === "deltaburst") {
    return {
      ammo_profile: 0x12,
      damage_profile: 0x01,
      health_profile: 0x0a,
    };
  }
  return {
    ammo_profile: 0x0a,
    damage_profile: 0x02,
    health_profile: 0x0a,
  };
}

function deriveConfigProfileValues(conn) {
  const profileFromConfigWrite = getConfigProfileForEntry(conn) || {};
  const profileFromSnapshot = extractProfileFromStartupRaw(conn?.last_snapshot?.raw);
  const profileFromLive = extractProfileFromStartupRaw(conn?.live_state?.startup_raw);
  const fallback = defaultProfileByBlasterType(conn);

  const ammo =
    toFiniteNumberOrNull(profileFromConfigWrite.ammo) ??
    toFiniteNumberOrNull(profileFromSnapshot?.ammo_profile) ??
    toFiniteNumberOrNull(profileFromLive?.ammo_profile) ??
    fallback.ammo_profile;
  const damage =
    toFiniteNumberOrNull(profileFromConfigWrite.damage) ??
    toFiniteNumberOrNull(profileFromSnapshot?.damage_profile) ??
    toFiniteNumberOrNull(profileFromLive?.damage_profile) ??
    fallback.damage_profile;
  const health =
    toFiniteNumberOrNull(profileFromConfigWrite.health) ??
    toFiniteNumberOrNull(profileFromSnapshot?.health_profile) ??
    toFiniteNumberOrNull(profileFromLive?.health_profile) ??
    fallback.health_profile;

  return {
    ammoProfile: clampInt(ammo, PROFILE_BYTE_MIN, PROFILE_BYTE_MAX, fallback.ammo_profile),
    damageProfile: clampInt(
      damage,
      PROFILE_BYTE_MIN,
      PROFILE_BYTE_MAX,
      fallback.damage_profile
    ),
    healthProfile: clampInt(
      health,
      PROFILE_BYTE_MIN,
      PROFILE_BYTE_MAX,
      fallback.health_profile
    ),
  };
}

function normAddress(address) {
  return String(address || "").toLowerCase();
}

function cacheScannedDevice(device) {
  const key = normAddress(device?.address);
  if (!key) return;
  const current = scannedDevicesByAddress.get(key) || {};
  scannedDevicesByAddress.set(key, {
    ...current,
    ...device,
    address: device.address || current.address,
    name: device.name || current.name || device.display_name || current.display_name || "unknown",
    rssi: device.rssi ?? current.rssi ?? null,
  });
}

function cacheDeviceFromConnection(conn) {
  const key = normAddress(conn?.address);
  if (!key) return;
  const current = scannedDevicesByAddress.get(key) || {};
  scannedDevicesByAddress.set(key, {
    ...current,
    address: conn.address || current.address,
    name: conn.name || current.name || conn.display_name || current.display_name || "unknown",
    rssi: current.rssi ?? null,
  });
}

function connectedAddressSet() {
  const connected = new Set();
  for (const entry of liveByAddress.values()) {
    if (String(entry.connection_state || "").toLowerCase() === "connected") {
      connected.add(normAddress(entry.address));
    }
  }
  return connected;
}

function renderCachedDevices() {
  const connected = connectedAddressSet();
  const devices = Array.from(scannedDevicesByAddress.values())
    .filter((dev) => !connected.has(normAddress(dev.address)))
    .sort((a, b) => {
      const nameCmp = String(a.name || "").localeCompare(String(b.name || ""));
      if (nameCmp !== 0) return nameCmp;
      return String(a.address || "").localeCompare(String(b.address || ""));
    });
  renderDevices(devices);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function teamName(team) {
  if (team === 0) return t("team.red");
  if (team === 1) return t("team.blue");
  if (team === 2) return t("team.violet");
  return t("team.generic", { team });
}

function formatTeam(slot, team) {
  if (slot == null || team == null) return t("misc.empty");
  return t("team.slotFormat", { slot, team: teamName(Number(team)) });
}

function formatPercent(value) {
  if (value == null || Number.isNaN(Number(value))) return t("misc.empty");
  return t("misc.percent", { value: Number(value).toFixed(1) });
}

function formatEndReason(reason) {
  if (!reason) return t("misc.empty");
  const key = `game.endReason.${reason}`;
  if (typeof translations[key] === "string") {
    return t(key);
  }
  return reason;
}

function formatConnectionState(entry) {
  const rawState = String(entry?.connection_state || "").trim();
  const state = rawState.toLowerCase();
  const reason = String(entry?.last_disconnect_reason || "").toLowerCase();
  if (
    reason === "team_confirmation" &&
    ["awaiting_team_confirmation", "disconnected", "reconnecting"].includes(state)
  ) {
    return t("connection.state.awaitingTeamConfirmation");
  }
  const key = `connection.state.${state}`;
  if (state && typeof translations[key] === "string") {
    return t(key);
  }
  return rawState || t("misc.empty");
}

function formatConnectionAction(action, reason = "") {
  const rawAction = String(action || "").trim();
  const rawReason = String(reason || "").trim();
  if (rawReason.toLowerCase() === "team_confirmation") {
    return t("connection.action.teamConfirmationWait");
  }
  const key = `connection.action.${rawAction}`;
  if (rawAction && typeof translations[key] === "string") {
    return t(key);
  }
  return rawAction || t("state.connectionActionFallback");
}

function displayName(entry) {
  return (
    entry.display_name ||
    entry.local_name ||
    entry.name ||
    entry.address ||
    t("state.unknown")
  );
}

function formatBlasterType(value) {
  const text = String(value || "").trim();
  if (!text) return t("table.unknownType");
  const lower = text.toLowerCase();
  if (lower === "alphapoint") return t("blasterType.alphaPoint");
  if (lower === "deltaburst") return t("blasterType.deltaBurst");
  if (lower === "unknown") return t("table.unknownType");
  return text;
}

function formatHexByte(value) {
  if (value == null || Number.isNaN(Number(value))) return t("misc.empty");
  return `0x${Number(value).toString(16).padStart(2, "0")}`;
}

function formatHexByteWithDec(value) {
  if (value == null || Number.isNaN(Number(value))) return t("misc.empty");
  const n = Number(value);
  return `0x${n.toString(16).padStart(2, "0")} (${n})`;
}

function toFiniteNumberOrNull(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function extractConfigProfileFromDerived(derived) {
  if (!derived || derived.msg_id !== "0x36") return null;
  const ammo = toFiniteNumberOrNull(derived.ammo_profile);
  const damage = toFiniteNumberOrNull(derived.damage_profile);
  const health = toFiniteNumberOrNull(derived.health_profile);
  if (ammo == null && damage == null && health == null) return null;
  return {
    family_hint: String(derived.family_hint || ""),
    ammo,
    damage,
    health,
    level: toFiniteNumberOrNull(derived.level),
    tail: toFiniteNumberOrNull(derived.tail_profile),
    byte7: toFiniteNumberOrNull(derived.byte7_selector),
  };
}

function getConfigProfileForEntry(entry) {
  if (entry?.config_profile) return entry.config_profile;
  const key = normAddress(entry?.address);
  if (!key) return null;
  const fromLive = liveByAddress.get(key)?.config_profile;
  return fromLive || null;
}

function formatCompactConfigProfile(profile) {
  if (!profile) return null;
  const hp = toFiniteNumberOrNull(profile.health);
  const dmg = toFiniteNumberOrNull(profile.damage);
  const ammo = toFiniteNumberOrNull(profile.ammo);
  if (hp == null && dmg == null && ammo == null) return null;
  const hpText = hp == null ? "-HP" : `${hp}HP`;
  const dmgText = dmg == null ? "-D" : `${dmg}D`;
  const ammoText = ammo == null ? "-A" : `${ammo}A`;
  return `${hpText} | ${dmgText} | ${ammoText}`;
}

function renderBlasterTypeCell(entry) {
  const ls = entry?.live_state || {};
  const snap = entry?.last_snapshot || {};
  const type = formatBlasterType(
    entry?.blaster_type || ls.startup_blaster_type || snap.blaster_type
  );
  const { ammoProfile, damageProfile, healthProfile } = deriveConfigProfileValues(entry);
  const compact = formatCompactConfigProfile({
    ammo: ammoProfile,
    damage: damageProfile,
    health: healthProfile,
  });
  return (
    `<div class="blaster-type-main">${escapeHtml(type)}</div>` +
    `<div class="blaster-type-sub">${escapeHtml(compact)}</div>`
  );
}

function buildConfigWriteProfileLine(packet) {
  const derived = packet?.derived || {};
  if (derived.msg_id !== "0x36") return null;
  const family = String(derived.family_hint || t("table.unknownType"));
  const level = Number.isFinite(Number(derived.level))
    ? String(Number(derived.level))
    : t("misc.empty");
  const nameA = formatHexByte(derived.name_a);
  const nameB = formatHexByte(derived.name_b);
  return [
    "config_profile:",
    `family=${family}`,
    `byte7=${formatHexByteWithDec(derived.byte7_selector)}`,
    `ammo=${formatHexByteWithDec(derived.ammo_profile)}`,
    `damage=${formatHexByteWithDec(derived.damage_profile)}`,
    `health=${formatHexByteWithDec(derived.health_profile)}`,
    `tail=${formatHexByteWithDec(derived.tail_profile)}`,
    `level=${level}`,
    `name=(${nameA}, ${nameB})`,
  ].join(" ");
}

function buildBlasterDebugText(entry) {
  const ls = entry?.live_state || {};
  const snap = entry?.last_snapshot || {};
  const type = formatBlasterType(
    entry?.blaster_type || ls.startup_blaster_type || snap.blaster_type
  );
  const level = snap.level ?? ls.startup_level ?? t("misc.empty");
  const nameA = snap.name_a ?? ls.startup_name_a;
  const nameB = snap.name_b ?? ls.startup_name_b;
  const raw = snap.raw || ls.startup_raw || t("misc.empty");
  return `profile type=${type} level=${level} name=(${formatHexByte(nameA)}, ${formatHexByte(nameB)}) raw=${raw}`;
}

function formatLinkState(entry) {
  const state = formatConnectionState(entry);
  const reconnect = entry.reconnect_count ?? 0;
  if (entry.last_error && entry.last_disconnect_reason !== "team_confirmation") {
    return t("connection.reconnectsWithError", { state, count: reconnect });
  }
  return t("connection.reconnects", { state, count: reconnect });
}

function pushLiveLine(line) {
  pushLiveBlock([line]);
}

function pushLiveBlock(lines) {
  if (!liveEventsBox) return;
  for (const line of lines) {
    liveLines.push(String(line));
  }
  liveLines.push(LIVE_SEPARATOR);
  if (liveLines.length > MAX_LIVE_LINES) {
    liveLines.splice(0, liveLines.length - MAX_LIVE_LINES);
  }
  liveEventsBox.textContent = liveLines.join("\n");
  liveEventsBox.scrollTop = liveEventsBox.scrollHeight;
}

function formatDebugValue(value) {
  if (value == null) return t("misc.empty");
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch (_err) {
    return String(value);
  }
}

function buildPacketDebugLines(ts, address, packet, liveState) {
  const safeAddress = address || t("misc.empty");
  const p = packet || {};
  const lines = [];
  lines.push(`${ts} [${safeAddress}] packet ${formatDebugValue(p.direction || "?")} raw=${formatDebugValue(p.raw)}`);
  lines.push(`decoded: ${formatDebugValue(p.decoded)}`);
  lines.push(`derived: ${formatDebugValue(p.derived)}`);
  const configProfile = buildConfigWriteProfileLine(p);
  if (configProfile) {
    lines.push(configProfile);
  }
  if (liveState != null) {
    lines.push(`live_state: ${formatDebugValue(liveState)}`);
  }
  return lines;
}

function upsertLiveEntry(entry) {
  const key = normAddress(entry.address);
  if (!key) return;
  const current = liveByAddress.get(key) || {};
  const merged = {
    ...current,
    ...entry,
  };
  if (entry.live_state == null && current.live_state != null) {
    merged.live_state = current.live_state;
  }
  if (entry.last_snapshot == null && current.last_snapshot != null) {
    merged.last_snapshot = current.last_snapshot;
  }
  if (entry.blaster_type == null && current.blaster_type != null) {
    merged.blaster_type = current.blaster_type;
  }
  merged.display_name = merged.display_name || merged.local_name || merged.name || merged.address;
  liveByAddress.set(key, merged);
}

function removeLiveEntry(address) {
  liveByAddress.delete(normAddress(address));
}

function renderLiveStatus() {
  liveStatusBody.innerHTML = "";
  const items = Array.from(liveByAddress.values()).sort((a, b) =>
    (a.address || "").localeCompare(b.address || "")
  );
  for (const item of items) {
    const ls = item.live_state || {};
    const shots =
      ls.shot_count == null ? (ls.trigger_count ?? 0) : Number(ls.shot_count);
    const ammo =
      ls.last_ammo_counter == null
        ? t("misc.empty")
        : String(Number(ls.last_ammo_counter));
    const life =
      ls.last_life_counter == null
        ? t("misc.empty")
        : String(Number(ls.last_life_counter));
    const status =
      ls.last_status_word == null
        ? t("misc.empty")
        : `0x${Number(ls.last_status_word).toString(16).padStart(4, "0")}`;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(displayName(item))}</td>
      <td>${renderBlasterTypeCell(item)}</td>
      <td>${escapeHtml(item.address || t("misc.empty"))}</td>
      <td>${escapeHtml(formatTeam(item.assigned_slot, item.assigned_team))}</td>
      <td>${escapeHtml(formatLinkState(item))}</td>
      <td>${shots}</td>
      <td>${ls.reload_count ?? 0}</td>
      <td>${escapeHtml(ammo)}</td>
      <td>${escapeHtml(life)}</td>
      <td>${escapeHtml(status)}</td>
      <td>${escapeHtml(ls.last_event || t("misc.empty"))}</td>
    `;
    tr.addEventListener("click", () => {
      if (item.address) targetAddressInput.value = item.address;
      if (item.assigned_slot != null) {
        document.getElementById("team-slot").value = String(
          safeSlotValue(item.assigned_slot)
        );
      }
      if (item.assigned_team != null) {
        document.getElementById("team-team").value = String(item.assigned_team);
      }
      if (item.auto_reconnect != null) {
        document.getElementById("auto-reconnect-enabled").checked = Boolean(
          item.auto_reconnect
        );
      }
    });
    liveStatusBody.appendChild(tr);
  }
}

function renderSetupTable(connections) {
  setupBody.innerHTML = "";
  const sorted = [...connections].sort((a, b) =>
    (a.address || "").localeCompare(b.address || "")
  );
  for (const conn of sorted) {
    const tr = document.createElement("tr");
    const safeAddress = conn.address || "";
    const slotValue = safeSlotValue(conn.assigned_slot);
    const teamValue = conn.assigned_team == null ? 2 : Number(conn.assigned_team);
    const localNameValue = conn.local_name || "";
    const levelValue = deriveLevelValue(conn);
    const { nameAIndex, nameBIndex } = deriveConfigNameIndices(conn);
    const { ammoProfile, damageProfile, healthProfile } = deriveConfigProfileValues(conn);

    tr.innerHTML = `
      <td>
        <div class="setup-cell-stack">
          <input class="setup-name-input" type="text" maxlength="32" value="${escapeHtml(localNameValue)}" placeholder="${escapeHtml(t("placeholder.localName"))}">
          <button type="button" class="setup-name-save setup-icon-btn" title="${escapeHtml(t("button.saveName"))}" aria-label="${escapeHtml(t("button.saveName"))}">&#128190;</button>
        </div>
      </td>
      <td>${escapeHtml(conn.name || t("state.unknown"))}</td>
      <td>${renderBlasterTypeCell(conn)}</td>
      <td class="setup-address-cell">${escapeHtml(safeAddress)}</td>
      <td>
        <div class="setup-cell-stack">
          <div class="setup-team-inline">
            <input class="setup-slot-input" type="number" min="${SLOT_MIN}" max="${SLOT_MAX}" value="${slotValue}">
            <select class="setup-team-select">
              <option value="0"${teamValue === 0 ? " selected" : ""}>${escapeHtml(t("team.red"))}</option>
              <option value="1"${teamValue === 1 ? " selected" : ""}>${escapeHtml(t("team.blue"))}</option>
              <option value="2"${teamValue === 2 ? " selected" : ""}>${escapeHtml(t("team.violet"))}</option>
            </select>
          </div>
          <button type="button" class="setup-team-save setup-icon-btn" title="${escapeHtml(t("button.saveTeam"))}" aria-label="${escapeHtml(t("button.saveTeam"))}">&#128190;</button>
        </div>
      </td>
      <td>
        <div class="setup-cell-stack setup-config-cell">
          <table class="setup-config-table" aria-label="${escapeHtml(t("table.blasterConfig"))}">
            <thead>
              <tr>
                <th>${escapeHtml(t("table.level"))}</th>
                <th>${escapeHtml(t("table.life"))}</th>
                <th>${escapeHtml(t("table.damage"))}</th>
                <th>${escapeHtml(t("table.ammo"))}</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>
                  <select class="setup-level-select">
                    <option value="1"${levelValue === 1 ? " selected" : ""}>1</option>
                    <option value="2"${levelValue === 2 ? " selected" : ""}>2</option>
                    <option value="3"${levelValue === 3 ? " selected" : ""}>3</option>
                    <option value="4"${levelValue === 4 ? " selected" : ""}>4</option>
                    <option value="5"${levelValue === 5 ? " selected" : ""}>5</option>
                  </select>
                </td>
                <td><input class="setup-health-input" type="number" min="${PROFILE_BYTE_MIN}" max="${PROFILE_BYTE_MAX}" value="${healthProfile}" title="${escapeHtml(t("label.healthProfile"))}"></td>
                <td><input class="setup-damage-input" type="number" min="${PROFILE_BYTE_MIN}" max="${PROFILE_BYTE_MAX}" value="${damageProfile}" title="${escapeHtml(t("label.damageProfile"))}"></td>
                <td><input class="setup-ammo-input" type="number" min="${PROFILE_BYTE_MIN}" max="${PROFILE_BYTE_MAX}" value="${ammoProfile}" title="${escapeHtml(t("label.ammoProfile"))}"></td>
              </tr>
            </tbody>
          </table>
          <div class="setup-icon-row">
            <button type="button" class="setup-config-save setup-icon-btn" title="${escapeHtml(t("button.saveConfig"))}" aria-label="${escapeHtml(t("button.saveConfig"))}">&#128190;</button>
            <button type="button" class="setup-config-reset setup-icon-btn setup-icon-btn-danger" title="${escapeHtml(t("button.resetConfig"))}" aria-label="${escapeHtml(t("button.resetConfig"))}">&#9851;</button>
          </div>
        </div>
      </td>
    `;

    const nameInput = tr.querySelector(".setup-name-input");
    const slotInput = tr.querySelector(".setup-slot-input");
    const teamSelect = tr.querySelector(".setup-team-select");
    const levelSelect = tr.querySelector(".setup-level-select");
    const healthInput = tr.querySelector(".setup-health-input");
    const damageInput = tr.querySelector(".setup-damage-input");
    const ammoInput = tr.querySelector(".setup-ammo-input");
    const saveNameButton = tr.querySelector(".setup-name-save");
    const saveTeamButton = tr.querySelector(".setup-team-save");
    const saveConfigButton = tr.querySelector(".setup-config-save");
    const resetConfigButton = tr.querySelector(".setup-config-reset");

    saveNameButton.addEventListener("click", (event) => {
      event.stopPropagation();
      runAction(
        () => setLocalNameForAddress(safeAddress, nameInput.value),
        { button: saveNameButton, busyText: "..." }
      );
    });

    saveTeamButton.addEventListener("click", (event) => {
      event.stopPropagation();
      const slot = safeSlotValue(slotInput.value);
      const team = Number(teamSelect.value);
      runAction(
        () => setTeamProfileByAddress(safeAddress, slot, team),
        { button: saveTeamButton, busyText: "..." }
      );
    });

    saveConfigButton.addEventListener("click", (event) => {
      event.stopPropagation();
      const level = Number(levelSelect.value);
      const profileOverrides = {
        health_profile: Number(healthInput.value),
        damage_profile: Number(damageInput.value),
        ammo_profile: Number(ammoInput.value),
      };
      runAction(
        () =>
          setLevelByAddress(
            safeAddress,
            level,
            nameAIndex,
            nameBIndex,
            profileOverrides
          ),
        { button: saveConfigButton, busyText: "..." }
      );
    });

    resetConfigButton.addEventListener("click", (event) => {
      event.stopPropagation();
      const confirmed = window.confirm(t("confirm.resetConfig"));
      if (!confirmed) return;
      const resetLevel = LEVEL_MIN;
      const resetProfile = defaultProfileByBlasterType(conn);
      runAction(
        async () => {
          const result = await setLevelByAddress(
            safeAddress,
            resetLevel,
            nameAIndex,
            nameBIndex,
            resetProfile
          );
          levelSelect.value = String(resetLevel);
          healthInput.value = String(resetProfile.health_profile);
          damageInput.value = String(resetProfile.damage_profile);
          ammoInput.value = String(resetProfile.ammo_profile);
          return result;
        },
        { button: resetConfigButton, busyText: "..." }
      );
    });

    tr.addEventListener("click", () => {
      targetAddressInput.value = safeAddress;
      document.getElementById("team-slot").value = String(
        safeSlotValue(slotInput.value)
      );
      document.getElementById("team-team").value = String(teamSelect.value);
    });

    setupBody.appendChild(tr);
  }
}

function renderRanking(snapshot) {
  rankingBody.innerHTML = "";
  const ranking = snapshot?.ranking || [];
  if (snapshot?.running) {
    const remaining = snapshot?.remaining_seconds;
    const remainingText =
      remaining == null
        ? t("misc.empty")
        : t("misc.secondsShort", { value: Math.max(0, Number(remaining) || 0) });
    setRankingState(
      t("ranking.running", {
        startedAt: tsToLocal(snapshot.started_at),
        remaining: remainingText,
        sessionId: snapshot.session_id,
      }),
      true
    );
  } else if (snapshot?.session_id != null) {
    setRankingState(
      t("ranking.ended", {
        reason: formatEndReason(snapshot.end_reason),
        endedAt: tsToLocal(snapshot.ended_at),
      }),
      false
    );
  } else {
    setRankingState(t("status.noActiveGame"), false);
  }
  for (const item of ranking) {
    const tr = document.createElement("tr");
    const name =
      item.display_name ||
      item.local_name ||
      item.name ||
      item.address ||
      t("misc.empty");
    tr.innerHTML = `
      <td>${item.rank ?? t("misc.empty")}</td>
      <td>${escapeHtml(name)}</td>
      <td>${escapeHtml(formatTeam(item.slot, item.team))}</td>
      <td>${item.shots ?? 0}</td>
      <td>${item.reloads ?? 0}</td>
      <td>${item.hits ?? t("misc.empty")}</td>
      <td>${item.kills ?? t("misc.empty")}</td>
      <td>${item.assists ?? t("misc.empty")}</td>
      <td>${escapeHtml(formatPercent(item.accuracy_percent))}</td>
      <td>${escapeHtml(formatConnectionState(item))}</td>
    `;
    tr.addEventListener("click", () => {
      if (item.address) {
        targetAddressInput.value = item.address;
      }
    });
    rankingBody.appendChild(tr);
  }
}

function renderRoundStats(snapshot) {
  if (!roundStatsBody || !gameSummaryPill) return;
  roundStatsBody.innerHTML = "";
  const slotStats = snapshot?.slot_stats || [];
  if (!snapshot || snapshot.session_id == null) {
    gameSummaryPill.textContent = t("status.noFinishedRound");
    gameSummaryPill.classList.add("error");
    return;
  }

  const totals = snapshot.totals || {};
  const totalsText = t("round.totals", {
    shots: totals.shots ?? 0,
    reloads: totals.reloads ?? 0,
    hits: totals.hits ?? t("misc.empty"),
    kills: totals.kills ?? t("misc.empty"),
    assists: totals.assists ?? t("misc.empty"),
  });

  if (snapshot.running) {
    gameSummaryPill.textContent = t("round.summary.running", { totals: totalsText });
  } else {
    const reason = formatEndReason(snapshot.end_reason);
    gameSummaryPill.textContent = t("round.summary.ended", {
      reason,
      totals: totalsText,
    });
  }
  gameSummaryPill.classList.remove("error");

  for (const item of slotStats) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${item.slot ?? t("misc.empty")}</td>
      <td>${escapeHtml(item.display_name || item.address || t("misc.empty"))}</td>
      <td>${escapeHtml(formatTeam(item.slot, item.team))}</td>
      <td>${item.hits ?? t("misc.empty")}</td>
      <td>${item.kills ?? t("misc.empty")}</td>
      <td>${item.assists ?? t("misc.empty")}</td>
    `;
    tr.addEventListener("click", () => {
      if (item.address) {
        targetAddressInput.value = item.address;
      }
    });
    roundStatsBody.appendChild(tr);
  }
}

function applyRankingSnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== "object") return;
  lastRankingSnapshot = snapshot;
  renderRanking(snapshot);
  renderRoundStats(snapshot);
}

function applyConnectionList(connections) {
  const present = new Set();
  for (const conn of connections) {
    present.add(normAddress(conn.address));
    cacheDeviceFromConnection(conn);
    upsertLiveEntry({
      address: conn.address,
      name: conn.name,
      local_name: conn.local_name,
      display_name: conn.display_name,
      live_state: conn.live_state || {},
      assigned_slot: conn.assigned_slot,
      assigned_team: conn.assigned_team,
      connection_state: conn.connection_state,
      auto_reconnect: conn.auto_reconnect,
      reconnect_count: conn.reconnect_count,
      disconnect_count: conn.disconnect_count,
      last_disconnect_reason: conn.last_disconnect_reason,
      last_error: conn.last_error,
      connected_at: conn.connected_at,
      blaster_type: conn.blaster_type,
      last_snapshot: conn.last_snapshot,
    });
  }
  for (const key of Array.from(liveByAddress.keys())) {
    if (!present.has(key)) {
      liveByAddress.delete(key);
    }
  }
  const currentTarget = getTargetAddress();
  if (currentTarget && !present.has(normAddress(currentTarget))) {
    targetAddressInput.value = "";
    notificationsBox.textContent = t("error.noTargetAddressSet");
  }
  renderConnections(connections);
  renderSetupTable(connections);
  renderCachedDevices();
  renderLiveStatus();
}

function renderConnections(connections) {
  const body = document.getElementById("connections-body");
  body.innerHTML = "";
  const sorted = [...connections].sort((a, b) =>
    (a.address || "").localeCompare(b.address || "")
  );
  for (const conn of sorted) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(conn.display_name || conn.local_name || conn.name || t("state.unknown"))}</td>
      <td>${renderBlasterTypeCell(conn)}</td>
      <td>${escapeHtml(conn.address)}</td>
      <td>${escapeHtml(formatTeam(conn.assigned_slot, conn.assigned_team))}</td>
      <td>${escapeHtml(formatLinkState(conn))}</td>
      <td>${escapeHtml(tsToLocal(conn.connected_at))}</td>
      <td><button type="button" data-address="${escapeHtml(conn.address)}">${escapeHtml(t("button.disconnect"))}</button></td>
    `;
    const button = tr.querySelector("button");
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      runAction(() => disconnectDevice(conn.address));
    });
    tr.addEventListener("click", () => {
      targetAddressInput.value = conn.address;
      if (conn.assigned_slot != null) {
        document.getElementById("team-slot").value = String(
          safeSlotValue(conn.assigned_slot)
        );
      }
      if (conn.assigned_team != null) {
        document.getElementById("team-team").value = String(conn.assigned_team);
      }
      if (conn.auto_reconnect != null) {
        document.getElementById("auto-reconnect-enabled").checked = Boolean(
          conn.auto_reconnect
        );
      }
    });
    body.appendChild(tr);
  }
}

function handleLiveSnapshot(snapshot) {
  const conns = snapshot?.connections || [];
  applyConnectionList(conns);
}

function handleConnectionEvent(payload, ts) {
  if (payload.action === "connected" && payload.connection) {
    const conn = payload.connection;
    upsertLiveEntry({
      address: conn.address,
      name: conn.name,
      local_name: conn.local_name,
      display_name: conn.display_name,
      live_state: conn.live_state || {},
      assigned_slot: conn.assigned_slot,
      assigned_team: conn.assigned_team,
      connection_state: conn.connection_state,
      auto_reconnect: conn.auto_reconnect,
      reconnect_count: conn.reconnect_count,
      disconnect_count: conn.disconnect_count,
      last_disconnect_reason: conn.last_disconnect_reason,
      last_error: conn.last_error,
      connected_at: conn.connected_at,
      blaster_type: conn.blaster_type,
      last_snapshot: conn.last_snapshot,
    });
    cacheDeviceFromConnection(conn);
    renderCachedDevices();
    pushLiveLine(
      `${ts} ${t("live.connected", { address: conn.address || t("misc.empty") })}`
    );
    pushLiveLine(
      `${ts} [${conn.address || t("misc.empty")}] ${buildBlasterDebugText(conn)}`
    );
    refreshConnections().catch((err) => {
      reportError(err, { contextKey: "context.liveConnection", dedupeMs: 10000 });
    });
    return;
  }

  if (payload.action === "disconnected") {
    cacheScannedDevice({
      address: payload.address,
      name: payload.name || payload.display_name || "unknown",
      rssi: null,
    });
    removeLiveEntry(payload.address);
    renderCachedDevices();
    clearTargetIfMatches(payload.address);
    pushLiveLine(
      `${ts} ${t("live.disconnected", { address: payload.address || t("misc.empty") })}`
    );
    refreshConnections().catch((err) => {
      reportError(err, { contextKey: "context.liveConnection", dedupeMs: 10000 });
    });
    return;
  }

  if (!payload.address) {
    return;
  }

  const key = normAddress(payload.address);
  const existing = liveByAddress.get(key) || {};
  const derivedState =
    payload.connection_state != null
      ? payload.connection_state
      : payload.action === "team_confirmation_wait"
        ? "awaiting_team_confirmation"
        : payload.action === "lost"
        ? "disconnected"
        : payload.action === "reconnect_attempt"
          ? "reconnecting"
          : payload.action === "reconnected"
            ? "connected"
            : existing.connection_state ?? null;
  const derivedError =
    payload.error != null
      ? payload.error
      : payload.reason != null && payload.reason !== "team_confirmation"
        ? payload.reason
        : existing.last_error ?? null;

  upsertLiveEntry({
    address: payload.address,
    name: payload.name || existing.name,
    local_name: payload.local_name ?? existing.local_name ?? null,
    display_name: payload.display_name ?? existing.display_name ?? null,
    live_state: payload.live_state || existing.live_state || {},
    assigned_slot: payload.slot ?? existing.assigned_slot ?? null,
    assigned_team: payload.team ?? existing.assigned_team ?? null,
    connection_state: derivedState,
    auto_reconnect:
      payload.auto_reconnect ?? payload.enabled ?? existing.auto_reconnect ?? null,
    reconnect_count: payload.reconnect_count ?? existing.reconnect_count ?? null,
    disconnect_count: payload.disconnect_count ?? existing.disconnect_count ?? null,
    last_disconnect_reason:
      payload.reason ?? existing.last_disconnect_reason ?? null,
    last_error: derivedError,
  });
  pushLiveLine(
    `${ts} ${t("live.connectionAction", {
      address: payload.address || t("misc.empty"),
      action: formatConnectionAction(payload.action, payload.reason),
    })}`
  );
  const debugEntry = liveByAddress.get(normAddress(payload.address));
  if (debugEntry) {
    pushLiveLine(`${ts} [${payload.address}] ${buildBlasterDebugText(debugEntry)}`);
  }
  renderLiveStatus();
}

function handleLiveEvent(eventEnvelope) {
  const eventType = eventEnvelope?.type;
  const payload = eventEnvelope?.payload || {};
  const ts = tsToClock(eventEnvelope?.ts);

  if (eventType === "ranking") {
    applyRankingSnapshot(payload);
    return;
  }

  if (eventType === "notification") {
    const notif = payload.notification || {};
    upsertLiveEntry({
      address: payload.address,
      name: payload.name,
      local_name: payload.local_name,
      display_name: payload.display_name,
      live_state: payload.live_state || {},
    });
    renderLiveStatus();
    pushLiveBlock(
      buildPacketDebugLines(
        ts,
        payload.address || t("misc.empty"),
        payload.packet || {
          direction: "rx",
          raw: notif.raw || "",
          decoded: notif.decoded || "",
          derived: notif.derived || null,
        },
        payload.live_state || null
      )
    );
    return;
  }

  if (eventType === "tx_packet") {
    if (payload.address) {
      const existing = liveByAddress.get(normAddress(payload.address)) || {};
      const configProfile = extractConfigProfileFromDerived(
        payload.packet?.derived || null
      );
      upsertLiveEntry({
        address: payload.address,
        name: payload.name || existing.name,
        local_name: payload.local_name ?? existing.local_name ?? null,
        display_name: payload.display_name ?? existing.display_name ?? null,
        live_state: payload.live_state || existing.live_state || {},
        ...(configProfile ? { config_profile: configProfile } : {}),
      });
      renderLiveStatus();
    }
    pushLiveBlock(
      buildPacketDebugLines(
        ts,
        payload.address || t("misc.empty"),
        payload.packet || {},
        payload.live_state || null
      )
    );
    return;
  }

  if (eventType === "connection") {
    handleConnectionEvent(payload, ts);
    return;
  }

  if (eventType === "game_session") {
    pushLiveLine(
      `${ts} ${t("live.roundAction", {
        action: payload.action || t("state.roundActionFallback"),
      })}`
    );
    refreshRanking().catch((err) => {
      reportError(err, { contextKey: "context.liveRanking", dedupeMs: 10000 });
    });
    return;
  }

  if (eventType === "local_name" && payload.connection) {
    const conn = payload.connection;
    upsertLiveEntry({
      address: conn.address,
      name: conn.name,
      local_name: conn.local_name,
      display_name: conn.display_name,
      live_state: conn.live_state || {},
      assigned_slot: conn.assigned_slot,
      assigned_team: conn.assigned_team,
      connection_state: conn.connection_state,
      auto_reconnect: conn.auto_reconnect,
      reconnect_count: conn.reconnect_count,
      disconnect_count: conn.disconnect_count,
      last_error: conn.last_error,
      connected_at: conn.connected_at,
      blaster_type: conn.blaster_type,
      last_snapshot: conn.last_snapshot,
    });
    renderLiveStatus();
    refreshConnections().catch((err) => {
      reportError(err, { contextKey: "context.liveConnection", dedupeMs: 10000 });
    });
    pushLiveLine(
      `${ts} ${t("live.localNameUpdated", { address: conn.address || t("misc.empty") })}`
    );
    return;
  }

  if (payload.address) {
    const existing = liveByAddress.get(normAddress(payload.address)) || {};
    upsertLiveEntry({
      address: payload.address,
      name: payload.name || existing.name,
      local_name: payload.local_name ?? existing.local_name ?? null,
      display_name: payload.display_name ?? existing.display_name ?? null,
      live_state: payload.live_state || existing.live_state || {},
      blaster_type: payload.blaster_type ?? existing.blaster_type ?? null,
      last_snapshot: payload.last_snapshot ?? existing.last_snapshot ?? null,
      assigned_slot: payload.slot ?? existing.assigned_slot ?? null,
      assigned_team: payload.team ?? existing.assigned_team ?? null,
    });
    renderLiveStatus();
    pushLiveLine(`${ts} [${payload.address}] ${eventType}`);
  }
}

async function refreshHealth() {
  try {
    const health = await api("/api/health");
    setStatus(t("status.apiReachable"), true);
    setBluetoothEnableButtonVisibility(health);
    return health;
  } catch (err) {
    setStatus(t("status.apiUnreachable"), false);
    setBluetoothEnableButtonVisibility(null);
    reportError(err, { contextKey: "context.apiHealthcheck", dedupeMs: 15000 });
  }
}

function setBluetoothEnableButtonVisibility(health) {
  if (!enableBluetoothButton) return;
  const supported = health?.bluetooth_supported === true;
  const active = health?.bluetooth_active === true;
  const shouldShow = supported && !active;
  enableBluetoothButton.classList.toggle("hidden", !shouldShow);
}

function openLiveStream() {
  if (liveSource) {
    liveSource.close();
    liveSource = null;
  }

  setLiveState(t("status.liveConnecting"), true);
  liveSource = new EventSource("/api/live/stream");

  liveSource.addEventListener("open", () => {
    setLiveState(t("status.liveConnected"), true);
  });

  liveSource.addEventListener("snapshot", (event) => {
    try {
      const payload = JSON.parse(event.data);
      handleLiveSnapshot(payload);
      pushLiveLine(`${tsToClock(payload.generated_at)} ${t("live.snapshot")}`);
      refreshRanking().catch((err) => {
        reportError(err, { contextKey: "context.liveRanking", dedupeMs: 10000 });
      });
    } catch (err) {
      reportError(err, { contextKey: "context.liveStream", dedupeMs: 10000 });
    }
  });

  const eventTypes = [
    "notification",
    "tx_packet",
    "connection",
    "game_session",
    "startup",
    "volume",
    "config",
    "game_start",
    "team_profile",
    "local_name",
    "ranking",
  ];
  for (const eventType of eventTypes) {
    liveSource.addEventListener(eventType, (event) => {
      try {
        const envelope = JSON.parse(event.data);
        handleLiveEvent(envelope);
      } catch (err) {
        reportError(err, { contextKey: "context.liveStream", dedupeMs: 10000 });
      }
    });
  }

  liveSource.onerror = () => {
    setLiveState(t("status.liveRetrying"), false);
    reportError(new Error(t("error.liveReconnectInProgress")), {
      contextKey: "context.liveStream",
      dedupeMs: 15000,
    });
  };
}

function renderDevices(devices) {
  const body = document.getElementById("devices-body");
  body.innerHTML = "";
  for (const dev of devices) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(dev.name)}</td>
      <td>${escapeHtml(dev.address)}</td>
      <td>${escapeHtml(dev.rssi ?? "?")}</td>
      <td><button type="button" data-address="${escapeHtml(dev.address)}">${escapeHtml(t("button.connect"))}</button></td>
    `;
    const button = tr.querySelector("button");
    button.disabled = connectBusy;
    button.textContent = connectBusy ? t("button.connecting") : t("button.connect");
    button.addEventListener("click", () => {
      if (connectBusy) return;
      setConnectBusy(true);
      runAction(
        async () => {
          try {
            return await connectDevice(dev.address);
          } finally {
            setConnectBusy(false);
          }
        },
        { contextKey: "context.liveConnection" }
      );
    });
    body.appendChild(tr);
  }
}

async function scan() {
  if (scanBusy) {
    return { status: "busy" };
  }
  setScanBusy(true);
  const timeout = Number(document.getElementById("scan-timeout").value);
  const byName = document.getElementById("scan-by-name").checked;
  const name = document.getElementById("scan-name").value.trim() || "NerfV";
  try {
    const result = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({
        timeout,
        by_name: byName,
        name,
        expected_count: 0,
      }),
    });
    for (const dev of result.devices || []) {
      cacheScannedDevice(dev);
    }
    renderCachedDevices();
    return result;
  } catch (err) {
    if (isScanBusyError(err)) {
      setScanBusy(true, Math.max(3000, timeout * 1000));
    } else {
      setScanBusy(false);
    }
    throw err;
  } finally {
    if (!scanBusyTimer) {
      setScanBusy(false);
    }
  }
}

async function enableBluetooth() {
  return api("/api/bluetooth/enable", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

async function refreshConnections() {
  const result = await api("/api/connections");
  const connections = result.connections || [];
  applyConnectionList(connections);
  return result;
}

async function refreshRanking() {
  const result = await api("/api/game/ranking");
  applyRankingSnapshot(result);
  return result;
}

async function connectDevice(address) {
  const cached = scannedDevicesByAddress.get(normAddress(address));
  const result = await api("/api/connect", {
    method: "POST",
    body: JSON.stringify({ address, timeout: 15.0 }),
  });
  cacheScannedDevice({
    ...(cached || {}),
    ...(result.connection || {}),
    address,
  });
  targetAddressInput.value = address;
  await refreshConnections();
  renderCachedDevices();
  return result;
}

async function disconnectDevice(address) {
  const known = liveByAddress.get(normAddress(address));
  if (known) {
    cacheDeviceFromConnection(known);
  }
  const result = await api(`/api/disconnect/${encodeURIComponent(address)}`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  clearTargetIfMatches(address);
  await refreshConnections();
  renderCachedDevices();
  await refreshRanking();
  return result;
}

async function setLocalNameForAddress(address, localNameValue) {
  const localName = String(localNameValue || "").trim();
  const payload = localName.length === 0 ? { local_name: null } : { local_name: localName };
  const result = await api(`/api/local-name/${encodeURIComponent(address)}`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  await refreshConnections();
  return result;
}

async function setAutoReconnect() {
  const address = mustTargetAddress();
  const enabled = document.getElementById("auto-reconnect-enabled").checked;
  const result = await api(`/api/reconnect/auto/${encodeURIComponent(address)}`, {
    method: "POST",
    body: JSON.stringify({ enabled }),
  });
  await refreshConnections();
  return result;
}

async function reconnectNow() {
  const address = mustTargetAddress();
  const result = await api(`/api/reconnect/${encodeURIComponent(address)}`, {
    method: "POST",
    body: JSON.stringify({ timeout: 15.0 }),
  });
  await refreshConnections();
  return result;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForBackendHealthy(timeoutMs = 30000, intervalMs = 500) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      await api("/api/health");
      return true;
    } catch (_err) {
      // keep waiting until timeout
    }
    await sleep(intervalMs);
  }
  return false;
}

async function restartBackend() {
  const confirmed = window.confirm(t("confirm.restartBackend"));
  if (!confirmed) {
    return { status: "cancelled" };
  }

  const result = await api("/api/server/restart", {
    method: "POST",
    body: JSON.stringify({}),
  });

  setStatus(t("status.apiUnknown"), true);
  setLiveState(t("status.liveRetrying"), false);
  if (liveSource) {
    liveSource.close();
    liveSource = null;
  }

  const healthy = await waitForBackendHealthy(30000, 500);
  if (!healthy) {
    const err = new Error(t("error.backendRestartTimeout"));
    err.status = 504;
    err.detail = t("error.backendRestartTimeout");
    throw err;
  }

  openLiveStream();
  await refreshConnections();
  await refreshRanking();
  return result;
}

async function startup() {
  const address = mustTargetAddress();
  const volume = Number(document.getElementById("startup-volume").value);
  return api(`/api/startup/${encodeURIComponent(address)}`, {
    method: "POST",
    body: JSON.stringify({ volume }),
  });
}

async function setVolume() {
  const address = mustTargetAddress();
  const volume = Number(document.getElementById("set-volume").value);
  return api(`/api/volume/${encodeURIComponent(address)}`, {
    method: "POST",
    body: JSON.stringify({ volume }),
  });
}

async function setTeamProfileByAddress(address, slot, team) {
  const result = await api(`/api/team/${encodeURIComponent(address)}`, {
    method: "POST",
    body: JSON.stringify({ slot, team }),
  });
  await refreshConnections();
  return result;
}

async function setLevelByAddress(
  address,
  level,
  nameA,
  nameB,
  profileOverrides = null
) {
  const body = {
    level: clampInt(level, LEVEL_MIN, LEVEL_MAX, LEVEL_MIN),
    name_a: clampInt(nameA, 0, 49, 0),
    name_b: clampInt(nameB, 0, 49, 0),
  };
  if (profileOverrides && typeof profileOverrides === "object") {
    const ammo = Number(profileOverrides.ammo_profile);
    const damage = Number(profileOverrides.damage_profile);
    const health = Number(profileOverrides.health_profile);
    if (Number.isFinite(ammo)) {
      body.ammo_profile = clampInt(ammo, PROFILE_BYTE_MIN, PROFILE_BYTE_MAX, PROFILE_BYTE_MIN);
    }
    if (Number.isFinite(damage)) {
      body.damage_profile = clampInt(
        damage,
        PROFILE_BYTE_MIN,
        PROFILE_BYTE_MAX,
        PROFILE_BYTE_MIN
      );
    }
    if (Number.isFinite(health)) {
      body.health_profile = clampInt(
        health,
        PROFILE_BYTE_MIN,
        PROFILE_BYTE_MAX,
        PROFILE_BYTE_MIN
      );
    }
  }
  const result = await api(`/api/config/${encodeURIComponent(address)}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  await refreshConnections();
  return result;
}

async function setTeamProfile() {
  const address = mustTargetAddress();
  const slot = safeSlotValue(document.getElementById("team-slot").value);
  const team = Number(document.getElementById("team-team").value);
  return setTeamProfileByAddress(address, slot, team);
}

async function startGame() {
  const address = mustTargetAddress();
  const delay = Number(document.getElementById("game-delay").value);
  const durationSeconds = Number(document.getElementById("game-duration").value);
  const startupVolume = Number(document.getElementById("game-startup-volume").value);
  const forceStartup = document.getElementById("force-startup").checked;
  const result = await api(`/api/game/start/${encodeURIComponent(address)}`, {
    method: "POST",
    body: JSON.stringify({
      delay,
      duration_seconds: durationSeconds,
      startup_volume: startupVolume,
      force_startup: forceStartup,
    }),
  });
  if (result?.result?.ranking) {
    applyRankingSnapshot(result.result.ranking);
  } else {
    await refreshRanking();
  }
  return result;
}

async function startGameMulti(body) {
  const result = await api("/api/game/start-multi", {
    method: "POST",
    body: JSON.stringify(body),
  });
  const requestedCount = Number(result?.result?.requested_count ?? 0);
  const startedCount = Number(result?.result?.started_count ?? 0);
  const failureCount = Number(result?.result?.failure_count ?? 0);
  if (requestedCount >= 2 && startedCount < 2 && failureCount > 0) {
    const failures = Array.isArray(result?.result?.failures) ? result.result.failures : [];
    const first = failures[0];
    const detail = first
      ? `${first.address || "?"}: ${first.error || t("error.unknown")}`
      : t("error.unknown");
    const err = new Error(detail);
    err.status = 409;
    err.detail = detail;
    throw err;
  }
  if (result?.result?.ranking) {
    applyRankingSnapshot(result.result.ranking);
  } else {
    await refreshRanking();
  }
  return result;
}

async function startGameMultiAdmin() {
  const raw = document.getElementById("multi-addresses").value.trim();
  const addresses = raw
    ? raw.split(",").map((x) => x.trim()).filter((x) => x.length > 0)
    : [];
  const delay = Number(document.getElementById("game-delay").value);
  const durationSeconds = Number(document.getElementById("game-duration").value);
  const startupVolume = Number(document.getElementById("game-startup-volume").value);
  const forceStartup = document.getElementById("force-startup").checked;
  return startGameMulti({
    addresses,
    delay,
    duration_seconds: durationSeconds,
    startup_volume: startupVolume,
    force_startup: forceStartup,
  });
}

async function startGameMultiSetup() {
  const delay = Number(document.getElementById("game-delay").value || 0);
  const durationSeconds = Number(document.getElementById("setup-game-duration").value);
  const startupVolume = Number(document.getElementById("setup-game-startup-volume").value);
  const forceStartup = document.getElementById("setup-force-startup").checked;
  return startGameMulti({
    addresses: [],
    delay,
    duration_seconds: durationSeconds,
    startup_volume: startupVolume,
    force_startup: forceStartup,
  });
}

async function pollStatus() {
  const address = mustTargetAddress();
  return api(`/api/status/poll/${encodeURIComponent(address)}`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

async function collectStats() {
  const address = mustTargetAddress();
  return api(`/api/stats/${encodeURIComponent(address)}`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

async function closeSession() {
  const address = mustTargetAddress();
  return api(`/api/session/close/${encodeURIComponent(address)}`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

async function endGame() {
  const result = await api("/api/game/end", {
    method: "POST",
    body: JSON.stringify({ reason: "manual_stop" }),
  });
  if (result?.result) {
    applyRankingSnapshot(result.result);
  } else {
    await refreshRanking();
  }
  return result;
}

async function refreshNotifications() {
  const address = getTargetAddress();
  if (!address) {
    notificationsBox.textContent = t("error.noTargetAddressSet");
    return { items: [] };
  }
  if (!isTargetConnected(address)) {
    notificationsBox.textContent = t("error.deviceNotConnected");
    return { items: [] };
  }
  let result;
  try {
    result = await api(`/api/notifications/${encodeURIComponent(address)}`);
  } catch (err) {
    if (Number(err?.status) === 404) {
      clearTargetIfMatches(address);
      notificationsBox.textContent = t("error.deviceNotConnected");
      return { items: [] };
    }
    throw err;
  }
  const lines = (result.items || []).map((item) => {
    const time = new Date(item.ts * 1000).toLocaleTimeString(currentLanguage);
    return `${time}  ${item.raw}  ${item.decoded}`;
  });
  notificationsBox.textContent = lines.join("\n");
  return result;
}

async function runAction(fn, options = {}) {
  const button = options.button || null;
  const busyText = options.busyText || null;
  const contextKey = options.contextKey || "context.action";
  const originalText = button ? button.textContent : null;
  if (button) {
    button.disabled = true;
    if (busyText) {
      button.textContent = busyText;
    }
  }
  try {
    const result = await fn();
    setResponse(result);
  } catch (err) {
    reportError(err, { contextKey });
    setResponse(errorPayload(err, contextKey));
  } finally {
    if (button) {
      button.disabled = false;
      if (busyText && originalText != null) {
        button.textContent = originalText;
      }
    }
  }
}

function activateView(view) {
  currentView = view;
  for (const btn of document.querySelectorAll(".tab-btn")) {
    btn.classList.toggle("active", btn.dataset.viewTarget === view);
  }
  for (const pane of document.querySelectorAll(".view-pane")) {
    const isVisible = pane.dataset.view === view;
    pane.classList.toggle("hidden", !isVisible);
  }
  if (view === "setup") {
    refreshConnections().catch((err) => {
      reportError(err, { contextKey: "context.viewSetup", dedupeMs: 10000 });
    });
  }
  if (view === "game") {
    refreshRanking().catch((err) => {
      reportError(err, { contextKey: "context.viewGame", dedupeMs: 10000 });
    });
  }
}

function bindClick(id, handler) {
  const el = document.getElementById(id);
  if (!el) return;
  el.addEventListener("click", handler);
}

if (scanButton) {
  scanButton.addEventListener("click", () => {
    if (scanBusy) return;
    runAction(scan, { contextKey: "context.deviceScan" });
  });
}
if (enableBluetoothButton) {
  enableBluetoothButton.addEventListener("click", () => {
    runAction(async () => {
      const result = await enableBluetooth();
      await refreshHealth();
      return result;
    }, {
      button: enableBluetoothButton,
      busyText: t("button.enablingBluetooth"),
      contextKey: "context.bluetoothControl",
    });
  });
}
bindClick("btn-refresh-connections", () =>
  runAction(refreshConnections, { contextKey: "context.viewSetup" })
);
bindClick("btn-setup-start-multi", () =>
  runAction(startGameMultiSetup, {
    contextKey: "context.start",
    button: document.getElementById("btn-setup-start-multi"),
    busyText: t("button.startingRound"),
  })
);
bindClick("btn-refresh-ranking", () =>
  runAction(refreshRanking, { contextKey: "context.liveRanking" })
);
bindClick("btn-game-end", () => runAction(endGame, { contextKey: "context.action" }));
bindClick("btn-set-auto-reconnect", () =>
  runAction(setAutoReconnect, { contextKey: "context.liveConnection" })
);
bindClick("btn-reconnect", () =>
  runAction(reconnectNow, { contextKey: "context.liveConnection" })
);
bindClick("btn-restart-backend", () =>
  runAction(restartBackend, {
    contextKey: "context.backendRestart",
    button: document.getElementById("btn-restart-backend"),
    busyText: t("button.restartingBackend"),
  })
);
bindClick("btn-startup", () => runAction(startup, { contextKey: "context.start" }));
bindClick("btn-set-volume", () =>
  runAction(setVolume, { contextKey: "context.action" })
);
bindClick("btn-set-team", () =>
  runAction(setTeamProfile, { contextKey: "context.action" })
);
bindClick("btn-game-start", () => runAction(startGame, { contextKey: "context.start" }));
bindClick("btn-game-start-multi", () =>
  runAction(startGameMultiAdmin, {
    contextKey: "context.start",
    button: document.getElementById("btn-game-start-multi"),
    busyText: t("button.startingRound"),
  })
);
bindClick("btn-game-end-admin", () =>
  runAction(endGame, { contextKey: "context.action" })
);
bindClick("btn-poll-status", () =>
  runAction(pollStatus, { contextKey: "context.action" })
);
bindClick("btn-close-session", () =>
  runAction(closeSession, { contextKey: "context.action" })
);
bindClick("btn-refresh-notifications", () =>
  runAction(refreshNotifications, { contextKey: "context.notifications" })
);
if (clearErrorsButton) {
  clearErrorsButton.addEventListener("click", () => clearErrors());
}

if (languageSelect) {
  languageSelect.addEventListener("change", async () => {
    try {
      await loadLanguage(languageSelect.value);
    } catch (_err) {
      try {
        await loadLanguage(DEFAULT_LANGUAGE);
      } catch (__err) {}
    }
    applyTranslations();
    refreshHealth().catch((err) => {
      reportError(err, { contextKey: "context.apiHealthcheck", dedupeMs: 10000 });
    });
    refreshConnections().catch((err) => {
      reportError(err, { contextKey: "context.viewSetup", dedupeMs: 10000 });
    });
    refreshRanking().catch((err) => {
      reportError(err, { contextKey: "context.liveRanking", dedupeMs: 10000 });
    });
    refreshNotifications().catch((err) => {
      reportError(err, { contextKey: "context.notifications", dedupeMs: 10000 });
    });
  });
}

for (const viewButton of document.querySelectorAll(".tab-btn")) {
  viewButton.addEventListener("click", () => {
    activateView(viewButton.dataset.viewTarget);
  });
}

setInterval(() => {
  const auto = document.getElementById("auto-refresh")?.checked;
  if (!auto) return;
  refreshNotifications().catch((err) => {
    reportError(err, { contextKey: "context.autoRefreshNotifications", dedupeMs: 15000 });
  });
}, 2000);

setInterval(() => {
  if (currentView !== "game") return;
  refreshRanking().catch((err) => {
    reportError(err, { contextKey: "context.autoRefreshRanking", dedupeMs: 15000 });
  });
}, 1500);

async function bootstrap() {
  const preferredLanguage = detectLanguage();
  try {
    await loadLanguage(preferredLanguage);
  } catch (_err) {
    try {
      await loadLanguage(DEFAULT_LANGUAGE);
    } catch (__err) {}
  }
  applyTranslations();
  setStatus(t("status.apiUnknown"), true);
  setLiveState(t("status.liveOff"), true);
  refreshHealth().catch((err) => {
    reportError(err, { contextKey: "context.apiHealthcheck", dedupeMs: 10000 });
  });
  runAction(refreshConnections, { contextKey: "context.viewSetup" });
  runAction(refreshRanking, { contextKey: "context.liveRanking" });
  openLiveStream();
  activateView("setup");
}

bootstrap().catch((err) => {
  reportError(err, { contextKey: "context.action" });
  setResponse(errorPayload(err, "context.action"));
});

window.addEventListener("beforeunload", () => {
  if (liveSource) liveSource.close();
});


