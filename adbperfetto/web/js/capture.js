// capture.js: device channel rail + bench capture workspace.

import { api } from "./api.js";
import { t } from "./i18n.js";
import { createTape, fmtSize, fmtTime } from "./tape.js";

const ACTIVE = new Set(["starting", "recording", "stopping", "pulling"]);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(className, text, onClick) {
  const b = el("button", className, text);
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

function triggerButton(className, glyphClass, text, onClick) {
  const b = el("button", className);
  if (glyphClass) b.appendChild(el("span", "trigger-glyph " + glyphClass));
  b.appendChild(el("span", null, text));
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

function headline(s) {
  switch (s.status) {
    case "starting": return t("status.starting");
    case "recording":
      if (s.stop_requested) return t("btn.stopRequested");
      if (s.snapshot_busy) return t("btn.savingSnapshot");
      return "";
    case "stopping": return t("status.stopping");
    case "pulling":
      return s.ended_by_duration ? t("msg.limit")
                                 : t("status.pulling");
    default: return "";
  }
}

function deviceStatus(dev, orphanHidden) {
  const s = dev.session;
  if (s && ACTIVE.has(s.status)) {
    const key = s.status === "recording"
      ? (s.mode === "flight" ? "status.flight" : "status.recording")
      : `status.${s.status}`;
    return {
      key, cls: s.connected === false ? "amber" : "live",
      active: true,
    };
  }
  if (s) {
    return {
      key: s.status === "done" ? "status.done"
        : s.status === "failed" ? "status.failed" : "status.ended",
      cls: s.status === "failed" ? "amber" : "rule",
      active: false,
    };
  }
  if (dev.orphan && !(orphanHidden && orphanHidden.has(dev.serial))) {
    return { key: "status.recording", cls: "amber" };
  }
  if (dev.state === "unauthorized") {
    return { key: "state.unauthorized", cls: "amber" };
  }
  if (dev.state === "offline") {
    return { key: "state.offline", cls: "amber" };
  }
  if (dev.state === "disconnected") {
    return { key: "state.disconnected", cls: "amber" };
  }
  return { key: "state.device", cls: "idle" };
}

function elapsedS(session) {
  if (session.status === "starting") return 0;
  const end = session.stopped_at || Date.now() / 1000;
  return Math.max(0, end - session.started_at);
}

function wantMobilelog(dev, prefs) {
  if (!dev.info || !dev.info.has_mobilelog) return false;
  return prefs.mobilelog === undefined ? true : !!prefs.mobilelog;
}

function wantSf(prefs) {
  return !!prefs.sf;
}

function sfIntervalMs(prefs) {
  const raw = parseInt(prefs.sfIntervalMs, 10);
  return Number.isFinite(raw) && raw >= 50 ? raw : 200;
}

function startRecordLabel(dev, prefs) {
  const ml = wantMobilelog(dev, prefs);
  const sf = wantSf(prefs);
  if (ml && sf) return t("btn.startRecordMlSf");
  if (ml) return t("btn.startRecordMl");
  if (sf) return t("btn.startRecordSf");
  return t("btn.startRecord");
}

function safeFilename(value) {
  return String(value || "")
    .replace(/[^A-Za-z0-9._-]+/g, "_")
    .replace(/^[ ._]+|[ ._]+$/g, "") || "unknown";
}

function cleanLabel(value) {
  return String(value || "").trim();
}

function sameName(a, b) {
  const norm = (x) => cleanLabel(x).toLowerCase().replace(/[\s_-]+/g, "");
  return !norm(a) || !norm(b) || norm(a) === norm(b);
}

export function deviceLabels(dev) {
  const info = (dev && dev.info) || {};
  const name = cleanLabel(info.device_name);
  const model = cleanLabel(info.model);
  if (model && name && !sameName(model, name)) {
    return {
      railTitle: `${model} (${name})`,
      headTitle: model,
      headSub: name,
      hasRealName: true,
    };
  }
  const single = model || name;
  if (single) {
    return {
      railTitle: single,
      headTitle: single,
      headSub: "",
      hasRealName: true,
    };
  }
  const serial = (dev && dev.serial) || "";
  return {
    railTitle: serial,
    headTitle: serial,
    headSub: "",
    hasRealName: false,
  };
}

function emptyGuide(state) {
  const stage = el("div", "empty-stage");
  const wrap = el("div", "empty");
  const head = el("div", "empty-head");
  head.appendChild(el("div", "empty-title", t("empty.title")));
  const waiting = el("div", "empty-waiting");
  waiting.append(el("span", "pulse-dot"),
                 el("span", null, t("guide.waiting")));
  head.appendChild(waiting);
  wrap.appendChild(head);

  const steps = el("ol", "empty-steps");
  [t("guide.step1"), t("guide.step2"), t("guide.step3")]
    .forEach((text, i) => {
      const li = el("li");
      li.append(el("span", "step-num", String(i + 1)),
                el("span", null, text));
      steps.appendChild(li);
    });
  wrap.appendChild(steps);

  if (state && state.adb_ok === false) {
    const block = el("div", "block failed-block adb-error");
    block.appendChild(el("div", "block-title", t("adb.error.title")));
    if (state.adb_error) {
      block.appendChild(el("pre", "adb-error-detail", state.adb_error));
    }
    block.appendChild(el("div", "hint dim", t("adb.error.fix")));
    wrap.appendChild(block);
  }
  stage.appendChild(wrap);
  return stage;
}

export function createCapture(deps) {
  const ui = {
    railSig: "",
    panelSig: "",
    tape: null,
    tapeHost: null,
    refs: {},
    orphanHidden: new Set(),
    showAllSaved: new Set(),
    selected: null,
    state: null,
    presets: [],
    outputs: [],
    defaultPreset: null,
    flightLifetimeH: 24,
  };

  // ---------------------------------------------------------------- rail

  function railSignature(devices) {
    return JSON.stringify(devices.map((d) => [
      d.serial, d.state,
      d.info ? d.info.model : "",
      d.info ? d.info.device_name : "",
      d.session ? d.session.status : null,
      d.session ? d.session.mode : null,
      d.session ? d.session.connected : null,
      d.orphan ? 1 : 0,
    ]));
  }

  function sortDevices(devices) {
    return [...devices].sort((a, b) => {
      const act = (d) =>
        d.session && ACTIVE.has(d.session.status) ? 0 : 1;
      if (act(a) !== act(b)) return act(a) - act(b);
      return deviceLabels(a).railTitle.localeCompare(
        deviceLabels(b).railTitle);
    });
  }

  function renderRail() {
    deps.rail.textContent = "";
    const head = el("div", "rail-header");
    head.append(el("span", "rail-title", t("rail.phones")),
                el("span", "rail-count", String(ui.state.devices.length)));
    deps.rail.appendChild(head);

    if (!ui.state.devices.length) {
      deps.rail.appendChild(el("div", "rail-empty", t("rail.empty")));
      return;
    }
    for (const dev of sortDevices(ui.state.devices)) {
      const status = deviceStatus(dev, ui.orphanHidden);
      const labels = deviceLabels(dev);
      const row = el("button", "rail-row");
      row.dataset.serial = dev.serial;
      if (ui.selected && dev.serial === ui.selected.serial) {
        row.classList.add("selected");
      }
      row.appendChild(el("span", "rail-led " + status.cls));
      const body = el("span", "rail-body");
      const modelEl = el("span", "rail-model", labels.railTitle);
      modelEl.title = labels.railTitle;
      body.appendChild(modelEl);
      if (labels.hasRealName) {
        body.appendChild(el("span", "rail-serial", dev.serial));
      }
      const right = el("span", "rail-status");
      right.appendChild(el("span", "rail-word " + status.cls,
                           t(status.key)));
      right.appendChild(el("span", "rail-time"));
      row.append(body, right);
      row.addEventListener("click", () => deps.onSelect(dev.serial));
      deps.rail.appendChild(row);
    }
  }

  function updateRailTimes() {
    deps.rail.querySelectorAll(".rail-row").forEach((row) => {
      const dev = ui.state.devices.find(
        (d) => d.serial === row.dataset.serial);
      const timeEl = row.querySelector(".rail-time");
      if (!dev || !timeEl) return;
      const s = dev.session;
      timeEl.textContent = (s && ACTIVE.has(s.status))
        ? fmtTime(elapsedS(s)) : "";
    });
  }

  // ------------------------------------------------------------ helpers

  function metaRow(dev) {
    const meta = el("div", "meta");
    const info = dev.info || {};
    const pairs = [
      [t("meta.serial"), dev.serial],
      info.sdk ? [t("meta.android"),
                  `${info.release || ""} (SDK ${info.sdk})`] : null,
      info.perfetto_version
        ? [t("meta.perfetto"), info.perfetto_version] : null,
      info.has_mobilelog !== undefined
        ? [t("meta.mtklog"), info.has_mobilelog ? "✓" : "—"] : null,
      info.sdk ? [t("meta.flight"),
                  info.supports_clone ? t("meta.clone")
                                      : t("meta.oneShot")] : null,
    ];
    for (const pair of pairs) {
      if (!pair) continue;
      const item = el("span", "meta-item");
      item.append(el("span", "meta-label", pair[0]),
                  el("span", "meta-value", pair[1]));
      meta.appendChild(item);
    }
    return meta;
  }

  function getPrefs(serial) {
    return deps.getPrefs(serial);
  }

  function panelSignature(dev) {
    const s = dev.session || {};
    return JSON.stringify([
      dev.serial, dev.state,
      dev.info ? dev.info.model : "",
      dev.info ? dev.info.device_name : "",
      s.status, s.mode, s.connected,
      s.stop_requested ? 1 : 0,
      s.snapshot_busy ? 1 : 0,
      (s.snapshots || []).length,
      (s.checklist || []).length,
      (s.disconnects || []).length,
      s.mobilelog ? 1 : 0,
      s.sf ? 1 : 0,
      s.sf ? (s.sf.dump_count || 0) : 0,
      dev.orphan ? 1 : 0,
      ui.orphanHidden.has(dev.serial) ? 1 : 0,
      getPrefs(dev.serial),
      (ui.outputs || []).length,
      ui.outputs && ui.outputs[0] ? ui.outputs[0].path : "",
    ]);
  }

  function presetOptions(serial, presetId, draft, disabled) {
    const sel = document.createElement("select");
    sel.id = "cfg-preset";
    sel.disabled = !!disabled;
    const builtin = document.createElement("optgroup");
    builtin.label = t("cfg.builtin");
    const mine = document.createElement("optgroup");
    mine.label = t("cfg.myPresets");
    let found = presetId === "__draft__";
    for (const p of ui.presets) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.name;
      (p.builtin ? builtin : mine).appendChild(opt);
      if (p.id === presetId) found = true;
    }
    sel.appendChild(builtin);
    if (mine.children.length) sel.appendChild(mine);
    if (draft) {
      const opt = document.createElement("option");
      opt.value = "__draft__";
      opt.textContent = t("cfg.draft");
      sel.appendChild(opt);
    }
    sel.value = found ? presetId
      : (ui.defaultPreset || (ui.presets[0] && ui.presets[0].id));
    if (sel.value !== presetId && ui.presets.length) {
      deps.setPrefs(serial, { presetId: sel.value });
    }
    sel.addEventListener("change", () => {
      deps.setPrefs(serial, { presetId: sel.value });
      refreshConfigMeta(serial);
    });
    return sel;
  }

  function currentConfig(serial) {
    const prefs = getPrefs(serial);
    if (prefs.presetId === "__draft__") {
      const draft = deps.getDraft();
      if (draft) return { text: draft.text, preset_id: null };
    }
    return { preset_id: prefs.presetId, text: null };
  }

  async function refreshConfigMeta(serial) {
    const prefs = getPrefs(serial);
    let durationMs = null;
    let bufferKb = 0;
    if (prefs.presetId === "__draft__") {
      const draft = deps.getDraft();
      if (!draft) return;
      const dm = draft.text.match(/duration_ms:\s*(\d+)/);
      if (dm) durationMs = parseInt(dm[1], 10);
      bufferKb = [...draft.text.matchAll(/size_kb:\s*(\d+)/g)]
        .reduce((a, m) => a + parseInt(m[1], 10), 0);
    } else {
      const p = ui.presets.find((x) => x.id === prefs.presetId);
      if (!p) return;
      durationMs = p.duration_ms;
      bufferKb = p.buffer_kb || 0;
    }
    const size = bufferKb ? fmtSize(bufferKb * 1024) : "";
    if (ui.refs.buffer) {
      ui.refs.buffer.textContent =
        size ? t("record.buffer", { size }) : "";
    }
    if (ui.refs.memory) {
      ui.refs.memory.textContent =
        size ? t("flight.memory", { size }) : "";
    }
    if (ui.refs["ro-idle-buf"]) {
      ui.refs["ro-idle-buf"].textContent = size || "—";
    }
    if (ui.refs.duration && durationMs && !prefs.durationSet) {
      const mins = Math.max(1, Math.round(durationMs / 60000));
      ui.refs.duration.value = String(mins);
      if (ui.refs["ro-target"] && !prefs.noLimit &&
          (prefs.mode || "record") === "record") {
        ui.refs["ro-target"].textContent = fmtTime(mins * 60);
      }
    }
  }

  function tapeSpec(dev, prefs) {
    const s = dev.session;
    const mode = (s && s.mode) || prefs.mode || "record";
    if (!s) {
      const duration = prefs.noLimit
        ? null : (prefs.durationMin || 30) * 60000;
      return {
        mode, status: "idle",
        startedAt: Date.now() / 1000, elapsedS: 0,
        receivedAt: performance.now(),
        durationMs: mode === "record" ? duration : null,
        idleSpanS: mode === "record" && duration
          ? duration / 1000 : 600,
        disconnects: [], snapshots: [],
        ariaLabel: t("status.idle"),
      };
    }
    const parts = [t(`status.${s.status}`) || s.status,
                   fmtTime(elapsedS(s))];
    if (s.duration_ms) {
      parts.push(`/ ${fmtTime(s.duration_ms / 1000)}`);
    }
    if ((s.disconnects || []).length) {
      parts.push(t("phone.disconnected"));
    }
    return {
      mode, status: s.status,
      startedAt: s.started_at, elapsedS: elapsedS(s),
      receivedAt: performance.now(),
      durationMs: s.duration_ms,
      disconnects: s.disconnects || [],
      snapshots: (s.snapshots || []).map((x) => ({ at: x.at })),
      ariaLabel: parts.join(", "),
    };
  }

  // --------------------------------------------------------- sections

  function deviceHeader(dev) {
    const head = el("div", "dev-head");
    const main = el("div", "dev-head-main");
    const titleGroup = el("div", "dev-title-group");
    const labels = deviceLabels(dev);
    titleGroup.appendChild(el("h1", "dev-name", labels.headTitle));
    if (labels.headSub) {
      titleGroup.appendChild(el("span", "dev-model", labels.headSub));
    }
    const st = deviceStatus(dev, ui.orphanHidden);
    titleGroup.appendChild(el("span", "dev-badge " + st.cls, t(st.key)));
    main.append(titleGroup, metaRow(dev));
    head.appendChild(main);

    if (dev.state === "unauthorized") {
      head.appendChild(el("div", "hint amber", t("hint.unauthorized")));
    } else if (dev.state === "offline") {
      head.appendChild(el("div", "hint amber", t("hint.offline")));
    } else if (dev.state === "disconnected" &&
               !(dev.session && ACTIVE.has(dev.session.status))) {
      head.appendChild(el("div", "hint amber", t("hint.disconnected")));
    }
    if (dev.wifi) {
      head.appendChild(el("div", "hint amber", t("hint.wifi")));
    }
    return head;
  }

  function transportDeck(dev, prefs) {
    const s = dev.session;
    const orphanShown = dev.orphan && !s &&
      !ui.orphanHidden.has(dev.serial);
    const deck = el("div", "transport-deck");
    const top = el("div", "transport-top");
    const left = el("div", "transport-left");

    if (!s) {
      if (!orphanShown) {
        const mode = prefs.mode || "record";
        const label = mode === "record"
          ? startRecordLabel(dev, prefs)
          : t("btn.startFlight");
        const btn = triggerButton("btn primary big", "rec", label,
                                  () => startSession(dev, prefs, mode));
        if (dev.state !== "device") btn.disabled = true;
        left.appendChild(btn);
      } else {
        const row = el("div", "btn-row");
        row.append(
          triggerButton("btn primary big", "stop", t("btn.stopSave"),
            async () => {
              try {
                await api.adopt(dev.serial);
                await api.stop(dev.serial, false);
                deps.toast(t("toast.stopped"));
              } catch (e) { deps.toast(e.message); }
            }),
          button("btn quiet", t("btn.leaveRunning"), () => {
            ui.orphanHidden.add(dev.serial);
            ui.panelSig = "";
          }));
        left.appendChild(row);
        left.appendChild(el("div", "hint amber", t("orphan.text")));
      }
    } else if (s.status === "starting") {
      const b = triggerButton("btn primary big", "rec", t("btn.starting"));
      b.disabled = true;
      left.appendChild(b);
    } else if (s.status === "recording") {
      if (s.mode === "record") {
        const b = triggerButton(
          "btn primary big", "stop",
          s.stop_requested ? t("btn.stopRequested") : t("btn.stopSave"),
          () => stopSession(dev));
        if (s.stop_requested) b.disabled = true;
        left.appendChild(b);
      } else {
        const row = el("div", "btn-row");
        const snap = triggerButton(
          "btn primary big", "snap",
          s.snapshot_busy ? t("btn.savingSnapshot") : t("btn.saveSnapshot"),
          () => takeSnapshot(dev));
        snap.id = "btn-snapshot";
        if (s.snapshot_busy) snap.disabled = true;
        const stop = button("btn quiet", t("btn.stop"),
                            () => stopFlightModal(dev));
        row.append(snap, el("span", "key-hint", "S"), stop);
        left.appendChild(row);
      }
    } else if (s.status === "stopping") {
      const b = triggerButton("btn primary big", "stop", t("btn.stopping"));
      b.disabled = true;
      left.appendChild(b);
    } else if (s.status === "pulling") {
      const b = triggerButton("btn primary big", "stop", t("btn.pulling"));
      b.disabled = true;
      left.appendChild(b);
    } else {
      // Terminal state: primary CTA lives on the result card right below;
      // show calm status pill here so there are never two primary buttons.
      const st = deviceStatus(dev);
      const line = el("div", "btn-row");
      line.appendChild(el("span", "dev-badge " + st.cls, t(st.key)));
      if (s.preset_name) {
        line.appendChild(el("span", "field-meta", s.preset_name));
      }
      left.appendChild(line);
    }

    const notes = el("div", "transport-notes");
    const msg = el("div", "message");
    msg.id = "sess-msg";
    msg.setAttribute("aria-live", "polite");
    const detail = el("div", "message-detail");
    detail.id = "sess-detail";
    notes.append(msg, detail);

    if (s && s.status === "recording") {
      if (s.mode === "record" && !s.stop_requested && s.connected) {
        notes.appendChild(el("div", "hint dim", t("hint.unplug")));
      }
      if (s.mode === "flight" && s.one_shot &&
          (s.snapshots || []).length === 0) {
        notes.appendChild(el("div", "hint amber", t("flight.oneShot")));
      }
      if (s.mobilelog && s.mobilelog.paused) {
        notes.appendChild(el("div", "hint dim",
                             t("ml.paused", { names: s.mobilelog.paused })));
      }
    }
    left.appendChild(notes);

    const ro = readouts(dev, prefs);
    top.append(left, ro);
    deck.appendChild(top);

    ui.tapeHost = el("div", "tape-host");
    deck.appendChild(ui.tapeHost);
    return deck;
  }

  function readouts(dev, prefs) {
    const s = dev.session;
    const row = el("div", "readouts");

    if (!s) {
      const mode = prefs.mode || "record";
      const targetStr = mode === "record"
        ? (prefs.noLimit ? t("time.unlimited")
                         : fmtTime((prefs.durationMin || 30) * 60))
        : `${prefs.lifetimeH || ui.flightLifetimeH || 24} ${t("flight.hours")}`;
      const st = deviceStatus(dev, ui.orphanHidden);
      const idleDefs = [
        ["elapsed", t("readout.elapsed"), "0:00", "readout-value standby", true],
        ["target", mode === "record" ? t("readout.target") : t("flight.lifetime"),
         targetStr, "readout-value"],
        ["idle-buf", t("readout.buffer"), "—", "readout-value"],
        ["phone", t("readout.phone"), t(st.key),
         "readout-value " + (st.cls === "idle" ? "live" : "amber")],
      ];
      for (const [id, label, initText, valCls, big] of idleDefs) {
        const item = el("div", "readout" + (big ? " big" : ""));
        const value = el("div", valCls, initText);
        value.id = "ro-" + id;
        item.append(value, el("div", "readout-label", label));
        row.appendChild(item);
      }
      return row;
    }

    const mode = s.mode || "record";
    const defs = mode === "record"
      ? [["elapsed", t("readout.elapsed"), true],
         ["size", t("readout.size")]]
      : [["elapsed", t("readout.elapsed"), true],
         ["snaps", t("readout.snapshots")]];
    if (ACTIVE.has(s.status)) {
      if (mode === "record") {
        if (s.mobilelog) defs.push(["mlog", t("readout.mobilelog")]);
        if (s.sf) defs.push(["sf", t("readout.sf")]);
      } else {
        defs.push(["memory", t("readout.memory")]);
      }
      defs.push(["phone", t("readout.phone")]);
    }
    for (const [id, label, big] of defs) {
      const item = el("div", "readout" + (big ? " big" : ""));
      const value = el("div", "readout-value");
      value.id = "ro-" + id;
      item.append(value, el("div", "readout-label", label));
      row.appendChild(item);
    }
    return row;
  }

  function segmented(dev, prefs) {
    const s = dev.session;
    const running = s && ACTIVE.has(s.status);
    const seg = el("div", "segmented");
    const mode = running ? s.mode : (prefs.mode || "record");
    for (const [key, label] of [["record", "seg.record"],
                                ["flight", "seg.flight"]]) {
      const b = el("button",
                   "seg-btn" + (mode === key ? " active" : ""),
                   t(label));
      b.disabled = !!running;
      b.addEventListener("click", () => {
        deps.setPrefs(dev.serial, { mode: key });
      });
      seg.appendChild(b);
    }
    return seg;
  }

  function configRow(dev, prefs, running) {
    const row = el("div", "config-row");
    row.appendChild(el("span", "field-label", t("config.label")));
    const sel = presetOptions(dev.serial, prefs.presetId,
                              deps.getDraft(), running);
    row.appendChild(sel);
    row.appendChild(button("btn quiet small", t("config.edit"), () => {
      deps.openConfigs(sel.value === "__draft__" ? null : sel.value);
    }));
    row.appendChild(button("btn quiet small", t("config.preview"),
                           () => openPreview(dev, prefs)));
    return row;
  }

  async function openPreview(dev, prefs) {
    const mode = (dev.session && ACTIVE.has(dev.session.status))
      ? dev.session.mode : (prefs.mode || "record");
    const cfg = currentConfig(dev.serial);
    try {
      let text = cfg.text;
      if (!text) {
        const preset = await api.preset(cfg.preset_id);
        text = preset.text;
      }
      const result = await api.preview({
        text, mode,
        duration_min: mode === "record" && !prefs.noLimit
          ? parseFloat(ui.refs.duration?.value || "0") || null : null,
        lifetime_h: mode === "flight"
          ? parseFloat(ui.refs.lifetime?.value || "0") || null : null,
        serial: dev.serial,
      });
      const modal = deps.showModal({
        title: t("modal.preview.title"),
        bodyHtml: '<pre class="modal-code"></pre>',
        wide: true,
        buttons: [
          [t("btn.copy"), async (close) => {
            await navigator.clipboard.writeText(result.text);
            deps.toast(t("toast.pathCopied"));
            close();
          }],
          [t("btn.confirm"), (close) => close(), "primary"],
        ],
      });
      modal.box.querySelector(".modal-code").textContent = result.text;
    } catch (e) {
      deps.toast(e.message);
    }
  }

  function inputsRow(dev, prefs) {
    const s = dev.session;
    const running = s && ACTIVE.has(s.status);
    const mode = running ? s.mode : (prefs.mode || "record");
    const row = el("div", "inputs-row");
    if (mode === "record") {
      const dur = document.createElement("input");
      dur.type = "number";
      dur.id = "inp-duration";
      dur.min = "1";
      dur.max = "1440";
      dur.value = String(prefs.durationMin || 30);
      dur.disabled = !!running || !!prefs.noLimit;
      dur.addEventListener("change", () => {
        deps.setPrefs(dev.serial, {
          durationMin: parseInt(dur.value, 10) || 30,
          durationSet: true,
        });
      });
      const capsule = el("span", "input-capsule");
      capsule.append(dur, el("span", "field-suffix", t("record.minutes")));
      const durLabel = el("label", "field");
      durLabel.append(el("span", "field-label", t("record.duration")), capsule);
      row.appendChild(durLabel);

      const noLimit = document.createElement("input");
      noLimit.type = "checkbox";
      noLimit.className = "switch";
      noLimit.checked = !!prefs.noLimit;
      noLimit.disabled = !!running;
      noLimit.addEventListener("change", () => {
        deps.setPrefs(dev.serial, { noLimit: noLimit.checked });
      });
      const nlLabel = el("label", "field check");
      nlLabel.append(noLimit, el("span", "field-label", t("record.noLimit")));
      row.appendChild(nlLabel);
      if (running) {
        const buffer = el("span", "field-meta");
        ui.refs.buffer = buffer;
        row.appendChild(buffer);
      }
    } else {
      const life = document.createElement("input");
      life.type = "number";
      life.id = "inp-lifetime";
      life.min = "1";
      life.max = "167";
      life.value = String(prefs.lifetimeH || ui.flightLifetimeH || 24);
      life.disabled = !!running;
      life.addEventListener("change", () => {
        deps.setPrefs(dev.serial, {
          lifetimeH: parseInt(life.value, 10) || 24,
        });
      });
      const capsule = el("span", "input-capsule");
      capsule.append(life, el("span", "field-suffix", t("flight.hours")));
      const lifeLabel = el("label", "field");
      lifeLabel.append(el("span", "field-label", t("flight.lifetime")),
                       capsule);
      row.appendChild(lifeLabel);
    }
    return row;
  }

  function mobilelogRow(dev, prefs) {
    const s = dev.session;
    const running = s && ACTIVE.has(s.status);
    const wrap = el("div", "mobilelog-row");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "switch";
    box.id = "inp-mobilelog";
    box.checked = wantMobilelog(dev, prefs);
    box.disabled = !!running;
    box.addEventListener("change", () => {
      deps.setPrefs(dev.serial, { mobilelog: box.checked });
    });
    const mlLabel = el("label", "field check");
    mlLabel.append(box, el("span", "field-label", t("record.mobilelog")));
    wrap.appendChild(mlLabel);
    wrap.appendChild(el("div", "hint dim mobilelog-hint",
                        t("record.mobilelogHint")));
    return wrap;
  }

  function sfModule(dev, prefs) {
    const s = dev.session;
    const running = s && ACTIVE.has(s.status);
    const mode = running ? s.mode : (prefs.mode || "record");
    const card = el("section", "module-card sf-card");
    const head = el("div", "module-head");
    head.appendChild(el("span", "module-title", t("sf.title")));
    if (running) {
      head.appendChild(el("span", "module-badge", t("sf.locked")));
    }
    card.appendChild(head);

    const body = el("div", "module-body");
    if (mode !== "record") {
      body.appendChild(el("div", "hint dim", t("record.sfHint")));
      card.appendChild(body);
      return card;
    }

    const intervalRow = el("div", "param-row");
    const interval = document.createElement("input");
    interval.type = "number";
    interval.id = "inp-sf-interval";
    interval.min = "50";
    interval.max = "60000";
    interval.step = "50";
    interval.value = String(sfIntervalMs(prefs));
    interval.disabled = !!running;
    interval.addEventListener("change", () => {
      deps.setPrefs(dev.serial, {
        sfIntervalMs: parseInt(interval.value, 10) || 200,
      });
    });
    const capsule = el("span", "input-capsule");
    capsule.append(interval, el("span", "field-suffix", t("record.sfMs")));
    const intervalLabel = el("label", "field");
    intervalLabel.append(
      el("span", "field-label", t("record.sfInterval")), capsule);
    intervalRow.appendChild(intervalLabel);
    body.appendChild(intervalRow);

    const wrap = el("div", "sf-row");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "switch";
    box.id = "inp-sf";
    box.checked = wantSf(prefs);
    box.disabled = !!running;
    box.addEventListener("change", () => {
      deps.setPrefs(dev.serial, { sf: box.checked });
    });
    const sfLabel = el("label", "field check");
    sfLabel.append(box, el("span", "field-label", t("record.sf")));
    wrap.appendChild(sfLabel);
    wrap.appendChild(el("div", "hint dim sf-hint", t("record.sfHint")));
    body.appendChild(wrap);

    if (s && s.sf && ACTIVE.has(s.status)) {
      body.appendChild(el(
        "div", "hint dim",
        `${t("readout.sf")}: ${s.sf.dump_count || 0}`));
    }
    card.appendChild(body);
    return card;
  }

  function setupModule(dev, prefs) {
    const s = dev.session;
    const running = s && ACTIVE.has(s.status);
    const mode = running ? s.mode : (prefs.mode || "record");

    const card = el("section", "module-card setup-card");
    const head = el("div", "module-head");
    head.appendChild(el("span", "module-title", t("setup.title")));
    if (running) {
      head.appendChild(el("span", "module-badge", t("setup.locked")));
    }
    card.appendChild(head);

    const body = el("div", "module-body");
    const modeGroup = el("div", "param-group");
    const modeRow = el("div", "param-row");
    modeRow.append(el("span", "field-label", t("setup.mode")),
                   segmented(dev, prefs));
    modeGroup.appendChild(modeRow);
    if (mode === "flight" && !s) {
      modeGroup.appendChild(el("div", "hint dim", t("flight.explainer")));
      if (dev.info && dev.info.sdk && !dev.info.supports_clone) {
        modeGroup.appendChild(el("div", "hint amber", t("flight.oneShot")));
      }
    }
    body.appendChild(modeGroup);

    body.appendChild(configRow(dev, prefs, running));
    body.appendChild(inputsRow(dev, prefs));
    if (mode === "record" && dev.info && dev.info.has_mobilelog) {
      body.appendChild(mobilelogRow(dev, prefs));
    }
    card.appendChild(body);
    return card;
  }

  function snapshotList(s) {
    const list = el("div", "snapshots");
    const snaps = s.snapshots || [];
    snaps.slice().reverse().forEach((snap) => {
      const when = new Date(snap.at * 1000);
      const hhmmss = [when.getHours(), when.getMinutes(), when.getSeconds()]
        .map((v) => String(v).padStart(2, "0")).join(":");
      const row = el("div", "snapshot-row");
      row.append(
        el("span", "snap-badge", `#${snap.index + 1}`),
        el("span", "snap-name", t("snapshot.row", { n: snap.index + 1 })),
        el("span", "snap-time", hhmmss),
        el("span", "snap-size", fmtSize(snap.size)),
        button("btn quiet small", t("btn.show"), () => {
          api.reveal(snap.local).catch((e) => deps.toast(e.message));
        }),
        button("btn quiet small", t("btn.copy"), async () => {
          await navigator.clipboard.writeText(snap.local);
          deps.toast(t("toast.pathCopied"));
        }));
      list.appendChild(row);
    });
    return list;
  }

  function artifactsModule(dev) {
    const card = el("section", "module-card saved");
    const model = (dev.info && dev.info.model) || dev.serial;
    const prefix = safeFilename(model);
    const mine = (ui.outputs || []).filter(
      (out) => out.name && out.name.startsWith(prefix));
    const snaps = (dev.session && dev.session.snapshots) || [];

    const head = el("div", "module-head");
    head.append(el("span", "module-title saved-title", t("saved.title")),
                el("span", "module-badge", String(mine.length)));
    card.appendChild(head);

    const body = el("div", "module-body artifacts-body");
    if (snaps.length) {
      const snapSec = el("div", "snapshots-section");
      snapSec.appendChild(el("div", "sub-section-head",
                             t("snapshots.title")));
      snapSec.appendChild(snapshotList(dev.session));
      body.appendChild(snapSec);
    }

    if (!mine.length) {
      const empty = el("div", "saved-empty");
      empty.append(el("div", "saved-empty-title", t("saved.emptyTitle")),
                   el("div", "saved-empty-hint", t("saved.emptyHint")));
      body.appendChild(empty);
      card.appendChild(body);
      return card;
    }

    const list = el("div", "saved-list");
    const expanded = ui.showAllSaved.has(dev.serial);
    const shown = expanded ? mine : mine.slice(0, 7);
    for (const out of shown) {
      const row = el("div", "saved-row");
      const isZip = /\.zip$/i.test(out.name);
      const nameNode = el("span", "saved-name", out.name);
      nameNode.title = out.name;
      row.append(
        el("span", "file-badge", isZip ? "ZIP" : "TRACE"),
        nameNode,
        el("span", "saved-size", fmtSize(out.size)),
        button("btn quiet small", t("btn.show"), () => {
          api.reveal(out.path).catch((e) => deps.toast(e.message));
        }),
        button("btn quiet small", t("btn.copy"), async () => {
          await navigator.clipboard.writeText(out.path);
          deps.toast(t("toast.pathCopied"));
        }));
      list.appendChild(row);
    }
    body.appendChild(list);

    if (mine.length > 7) {
      const foot = el("div", "saved-foot");
      const toggle = button("btn quiet small",
        expanded ? t("saved.showLess")
                 : t("saved.showAll", { n: mine.length }), () => {
        if (expanded) ui.showAllSaved.delete(dev.serial);
        else ui.showAllSaved.add(dev.serial);
        ui.panelSig = "";
      });
      toggle.id = "saved-toggle";
      foot.appendChild(toggle);
      body.appendChild(foot);
    }
    card.appendChild(body);
    return card;
  }

  // ------------------------------------------------- contextual callouts

  function statusSection(dev) {
    const s = dev.session;
    const sec = el("div", "status-area");
    if (!s) return sec;

    if (s.status === "recording" && !s.connected) {
      sec.appendChild(el("div", "block amber-block",
                         t("banner.disconnected")));
    } else if (s.status === "pulling") {
      const card = el("div", "pull-card");
      const wrap = el("div", "pullbar");
      const fill = el("div", "pullbar-fill");
      fill.id = "pullbar-fill";
      wrap.appendChild(fill);
      card.append(wrap, el("div", "hint dim", t("pull.note")));
      sec.appendChild(card);
    } else if (s.status === "done") {
      sec.appendChild(doneBlock(dev, s));
    } else if (s.status === "failed") {
      sec.appendChild(failedBlock(dev, s));
    } else if (s.status === "ended" || s.status === "abandoned") {
      const block = el("div", "block amber-block");
      block.appendChild(el("div", "block-text",
                           s.mode === "flight" ? t("flight.ended")
                                               : (s.message || t("status.ended"))));
      block.appendChild(dismissRow(dev));
      sec.appendChild(block);
    }
    return sec;
  }

  function dismissRow(dev) {
    const row = el("div", "btn-row");
    row.appendChild(button("btn quiet", t("btn.dismiss"), async () => {
      try { await api.dismiss(dev.serial); }
      catch (e) { deps.toast(e.message); }
    }));
    return row;
  }

  function doneBlock(dev, s) {
    const block = el("div", "block done-block");
    const head = el("div", "done-head");
    head.appendChild(el("span", "done-check", "✓"));
    const path = (s.outputs || [])[0];
    const info = el("div", "done-info");
    info.appendChild(el("div", "done-name",
                        path ? path.split(/[\\/]/).pop()
                             : t("done.title")));
    head.appendChild(info);
    block.appendChild(head);

    if ((s.checklist || []).length) {
      const clist = el("div", "check-list");
      for (const item of s.checklist) {
        const row = el("div", "check-row " + (item.ok ? "ok" : "bad"));
        row.append(el("span", "check-mark", item.ok ? "✓" : "✕"),
                   el("span", "check-label", item.label),
                   el("span", "check-size", fmtSize(item.size)));
        clist.appendChild(row);
      }
      block.appendChild(clist);
    }
    if ((s.checklist || []).some((c) => !c.ok)) {
      block.appendChild(el("div", "hint alert", t("check.missing")));
    }

    const row = el("div", "btn-row");
    row.append(
      button("btn primary", t("btn.showInFolder"), () => {
        if (path) {
          api.reveal(path).catch((e) => deps.toast(e.message));
        }
      }),
      button("btn quiet", t("btn.copyPath"), async () => {
        if (path) {
          await navigator.clipboard.writeText(path);
          deps.toast(t("toast.pathCopied"));
        }
      }),
      button("btn quiet", t("btn.newRecording"), async () => {
        try { await api.dismiss(dev.serial); }
        catch (e) { deps.toast(e.message); }
      }));
    block.appendChild(row);
    return block;
  }

  function failedBlock(dev, s) {
    const block = el("div", "block failed-block");
    block.appendChild(el("div", "block-title", t("failed.title")));
    if (s.error) {
      block.appendChild(el("pre", "failed-detail", s.error));
    }
    if (s.remote_path) {
      block.appendChild(el("div", "hint dim",
                           t("failed.onDevice", { path: s.remote_path })));
    }
    block.appendChild(dismissRow(dev));
    return block;
  }

  // ------------------------------------------------- actions

  async function startSession(dev, prefs, mode) {
    const cfg = currentConfig(dev.serial);
    try {
      if (mode === "record") {
        await api.record(dev.serial, {
          preset_id: cfg.preset_id, text: cfg.text,
          duration_min: prefs.noLimit
            ? null
            : parseFloat(ui.refs.duration?.value || "0") || null,
          mobilelog: wantMobilelog(dev, prefs),
          sf: wantSf(prefs),
          sf_interval_ms: sfIntervalMs(prefs),
        });
      } else {
        await api.flight(dev.serial, {
          preset_id: cfg.preset_id, text: cfg.text,
          lifetime_h: parseFloat(ui.refs.lifetime?.value || "0") || null,
        });
      }
      deps.toast(t("toast.started"));
    } catch (e) {
      deps.toast(e.message);
    }
  }

  async function stopSession(dev) {
    try { await api.stop(dev.serial, false); }
    catch (e) { deps.toast(e.message); }
  }

  async function takeSnapshot(dev) {
    try {
      await api.snapshot(dev.serial);
      deps.toast(t("toast.snapshotSaved"));
    } catch (e) { deps.toast(e.message); }
  }

  function stopFlightModal(dev) {
    const modal = deps.showModal({
      title: t("modal.stopFlight.title"),
      bodyHtml: '<label class="field check modal-check">' +
        '<input type="checkbox" class="switch" id="stop-save-snap" checked>' +
        '<span class="field-label"></span></label>',
      buttons: [
        [t("btn.cancel"), (close) => close()],
        [t("btn.stop"), async (close) => {
          const save = modal.box.querySelector("#stop-save-snap");
          close();
          try {
            await api.stop(dev.serial, save ? save.checked : true);
            deps.toast(t("toast.stopped"));
          } catch (e) { deps.toast(e.message); }
        }, "primary"],
      ],
    });
    modal.box.querySelector(".modal-check .field-label").textContent =
      t("modal.stopFlight.saveFirst");
  }

  // ------------------------------------------------- dynamic update

  function updateDynamic(dev) {
    const s = dev.session;
    const set = (id, text, cls) => {
      const node = ui.refs[id];
      if (!node) return;
      if (text !== undefined) node.textContent = text;
      if (cls !== undefined) node.className = cls;
    };
    const msg = document.getElementById("sess-msg");
    const detail = document.getElementById("sess-detail");
    if (s && msg) {
      msg.textContent = headline(s);
    } else if (msg) {
      msg.textContent = "";
    }
    if (detail) {
      detail.textContent = s &&
        (s.status === "starting" || s.status === "pulling")
        ? (s.message || "") : "";
    }
    if (!s) {
      if (ui.tape) {
        ui.tape.update(tapeSpec(dev, getPrefs(dev.serial)));
      }
      return;
    }
    set("ro-elapsed", fmtTime(elapsedS(s)));
    set("ro-size", s.size_bytes ? fmtSize(s.size_bytes) : "—");
    set("ro-snaps", String((s.snapshots || []).length));
    set("ro-memory",
        s.buffer_kb ? fmtSize(s.buffer_kb * 1024) : "—");
    set("ro-phone",
        s.connected ? t("phone.connected") : t("phone.disconnected"),
        "readout-value " + (s.connected ? "live" : "amber"));
    set("ro-mlog",
        s.mobilelog && s.mobilelog.running
          ? fmtSize(s.mobilelog.log_bytes) : "—");
    set("ro-sf",
        s.sf ? String(s.sf.dump_count || 0) : "—");
    const fill = document.getElementById("pullbar-fill");
    if (fill) {
      fill.style.width = `${Math.round((s.pull_progress || 0) * 100)}%`;
    }
    if (ui.tape) {
      ui.tape.update(tapeSpec(dev, getPrefs(dev.serial)));
    }
  }

  // ------------------------------------------------- main render

  function renderPanel() {
    const host = deps.main;
    const dev = ui.selected;
    ui.refs = {};
    host.textContent = "";
    if (!dev) {
      host.appendChild(emptyGuide(ui.state));
      return;
    }
    const prefs = getPrefs(dev.serial);
    const inner = el("div", "panel-inner");
    host.appendChild(inner);

    inner.appendChild(deviceHeader(dev));
    inner.appendChild(transportDeck(dev, prefs));
    inner.appendChild(statusSection(dev));

    const grid = el("div", "bench-grid");
    const mode = (dev.session && ACTIVE.has(dev.session.status))
      ? dev.session.mode : (prefs.mode || "record");
    if (mode === "record") {
      grid.append(setupModule(dev, prefs), sfModule(dev, prefs));
      const artifacts = artifactsModule(dev);
      artifacts.classList.add("artifacts-span");
      grid.appendChild(artifacts);
    } else {
      grid.append(setupModule(dev, prefs), artifactsModule(dev));
    }
    inner.appendChild(grid);

    ui.refs.duration = host.querySelector("#inp-duration");
    ui.refs.lifetime = host.querySelector("#inp-lifetime");
    ui.refs.sfInterval = host.querySelector("#inp-sf-interval");
    for (const id of ["elapsed", "size", "snaps", "memory", "phone",
                      "mlog", "sf", "target", "idle-buf"]) {
      ui.refs["ro-" + id] = host.querySelector("#ro-" + id);
    }

    if (ui.tape) ui.tape.destroy();
    ui.tape = createTape(ui.tapeHost);
    ui.tape.update(tapeSpec(dev, prefs));
    refreshConfigMeta(dev.serial);
  }

  return {
    setData({ presets, defaultPreset, flightLifetimeH, outputs }) {
      if (presets !== undefined) ui.presets = presets;
      if (defaultPreset !== undefined) ui.defaultPreset = defaultPreset;
      if (flightLifetimeH !== undefined) {
        ui.flightLifetimeH = flightLifetimeH;
      }
      if (outputs !== undefined) ui.outputs = outputs;
    },
    update(state, selectedSerial) {
      ui.state = state;
      const dev = state.devices.find(
        (d) => d.serial === selectedSerial) || null;
      const selectionChanged =
        (dev && dev.serial) !== (ui.selected && ui.selected.serial);
      ui.selected = dev;
      const railSig = railSignature(state.devices);
      if (railSig !== ui.railSig || selectionChanged) {
        ui.railSig = railSig;
        renderRail();
      }
      updateRailTimes();
      if (!dev) {
        if (ui.panelSig !== "empty") {
          ui.panelSig = "empty";
          renderPanel();
        }
        return;
      }
      const sig = panelSignature(dev);
      if (sig !== ui.panelSig) {
        ui.panelSig = sig;
        renderPanel();
      }
      updateDynamic(dev);
    },
    invalidate() {
      ui.railSig = "";
      ui.panelSig = "";
    },
    pageTitle() {
      const dev = ui.selected;
      if (dev && dev.session && ACTIVE.has(dev.session.status)) {
        const s = dev.session;
        const key = s.mode === "flight" ? "title.flight"
                                        : "title.recording";
        return t(key, { time: fmtTime(elapsedS(s)) });
      }
      return t("title.default");
    },
    requestSnapshot() {
      const dev = ui.selected;
      const s = dev && dev.session;
      if (s && s.mode === "flight" && s.status === "recording" &&
          s.connected) {
        takeSnapshot(dev);
      }
    },
  };
}
