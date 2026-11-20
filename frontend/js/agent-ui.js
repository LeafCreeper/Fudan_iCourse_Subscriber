import { loadContract, modelTools } from "../agent/contract.js";
import { contextStatus, newSession } from "../agent/context.js";
import { readCursorLocation } from "../agent/course-tools.js";
import { defaultConfig, ModelRequestError, modelUrl, presets, testConnection } from "../agent/model.js";
import { runTurn } from "../agent/run.js";
import { loadConfig, saveConfig, sessions } from "../agent/store.js";

const citationViews = { summary: "summary", transcript: "transcript", ocr: "ppt" };
const paneKey = "ics_agent_pane";
const readingViews = ["courses", "lectures", "detail"];
const desktopQuery = "(min-width: 760px), (min-width: 600px) and (pointer: fine)";
const maxPaneWidth = () => window.innerWidth - 310;
const quote = value => JSON.stringify(value);
const ready = config => Boolean(config.url && config.model && (config.provider === "custom" || config.apiKey));
const delay = () => new Promise(resolve => setTimeout(resolve, 0));
const historyDate = new Intl.DateTimeFormat("en-US", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "short", day: "numeric",
});

function listConversations(records) {
  const rows = [...records.values()].sort((a, b) => b.updatedAt - a.updatedAt)
    .map(({ id, title, updatedAt }) => {
      const date = new Date(updatedAt);
      const parts = Object.fromEntries(historyDate.formatToParts(date).map(({ type, value }) => [type, value]));
      return { id, title, dateTime: date.toISOString(), dateLabel: parts.month + " " + parts.day, year: parts.year };
    });
  return rows.map((row, i) => ({
    ...row, dateLabel: row.dateLabel + (rows[i + 1]?.year !== row.year ? ", " + row.year : ""),
  }));
}

export async function makeAgentUI(app) {
  const [spec, saved] = await Promise.all([loadContract(), sessions.list()]);
  const tools = modelTools(spec);
  const records = new Map(saved.map(record => [record.id, record]));
  const media = matchMedia(desktopQuery);
  const pane = JSON.parse(localStorage.getItem(paneKey) ?? '{"width":400,"closed":false}');
  const config = loadConfig(defaultConfig);
  let active = records.get(sessions.active) ?? saved.sort((a, b) => b.updatedAt - a.updatedAt)[0] ?? newSession();
  const citationNumbers = new Map();
  let citationIndex = 0;
  let controller;
  let lastPage;
  let drag;

  function currentPage() {
    if (app.view === "courses") return { view: "courses", courseIds: app.courses.map(course => course.course_id) };
    if (app.view === "lectures") return { view: "lectures", courseId: app.currentCourse.course_id };
    if (app.view === "detail" && app.currentLecture) return { view: "detail", lectureId: app.currentLecture.sub_id };
    return null;
  }

  function selectRecord(record) {
    if (record.run?.status === "running") {
      record.run.status = "paused";
      record.run.detail = "上次检索中断，可以继续。";
    }
    active = record;
    citationNumbers.clear();
    citationIndex = 0;
    sessions.active = active.id;
  }

  function formConfig(form) {
    const value = { ...form, url: form.url.trim(), model: form.model.trim(), apiKey: form.apiKey.trim(),
      contextChars: Number(form.contextChars), contextTokens: Number(form.contextTokens) };
    if (!Number.isSafeInteger(value.contextChars) || value.contextChars < 16000 ||
        !Number.isSafeInteger(value.contextTokens) || (value.contextTokens !== 0 && value.contextTokens < 16384)) {
      throw new ModelRequestError("字符预算至少为 16000。模型窗口请填 0 或至少 16384 token。", "configuration");
    }
    if (value.url || value.model || value.apiKey) {
      if (!value.model) throw new ModelRequestError("请输入模型 ID。", "configuration");
      modelUrl(value.url);
      if (value.provider !== "custom" && !value.apiKey) throw new ModelRequestError("请输入 API key。", "configuration");
    }
    return value;
  }

  function paneSave(ui) {
    localStorage.setItem(paneKey, JSON.stringify({ width: ui.width, closed: ui.closed }));
  }

  selectRecord(active);
  const state = {
    config,
    form: { ...config },
    providers: Object.entries(presets).map(([id, preset]) => ({ id, label: preset.label })),
    desktop: media.matches,
    width: pane.width,
    closed: pane.closed,
    messageList: [],
    liveText: "",
    answerStarted: false,
    lastQuestion: -1,
    conversations: [],
    activeId: active.id,
    title: active.title,
    draft: "",
    context: { line: "", percent: 0 },
    notice: "",
    operationError: "",
    settingsNote: "",
    testing: false,
    running: false,
    paused: false,
    historyOpen: false,
    chatMenu: null,
    chatAction: null,
    renameDraft: "",
    historySaving: false,

    connectMedia() {
      media.addEventListener("change", event => { this.desktop = event.matches; });
      this.sync();
    },

    get contextTone() {
      const { percent } = this.context;
      return percent >= 80 ? "high" : percent >= 60 ? "medium" : "low";
    },

    get configured() { return ready(this.config); },
    get onReadingPage() { return readingViews.includes(app.view); },
    get dock() { return this.desktop && this.onReadingPage; },
    get pageLabel() {
      if (!this.desktop) return "";
      const page = currentPage() ?? (app.view === "agent" ? lastPage : null);
      if (!page) return "";
      if (page.view === "courses") return "参考课程列表";
      if (page.view === "lectures") return "参考当前课程";
      return "参考当前课次";
    },

    rememberReading() {
      if (this.desktop && readingViews.includes(app.view)) lastPage = currentPage();
    },

    sync() {
      for (const entry of active.visible.slice(citationIndex)) {
        if (entry.role === "assistant") window.ICS.render.indexCitations(entry.text, citationNumbers);
      }
      citationIndex = active.visible.length;
      this.messageList = active.visible.slice();
      this.lastQuestion = active.visible.findLastIndex(item => item.role === "user");
      this.conversations = listConversations(records);
      this.activeId = active.id;
      this.title = active.title;
      this.context = contextStatus(active, tools, this.config);
      this.notice = active.run?.detail ?? "";
      this.paused = active.run?.status === "paused";
    },

    newChat() {
      selectRecord(newSession());
      this.historyOpen = false;
      this.closeChatMenu();
      this.draft = "";
      this.operationError = "";
      this.sync();
    },

    openChat(id) {
      if (this.running) return;
      selectRecord(records.get(id));
      this.draft = "";
      this.historyOpen = false;
      this.closeChatMenu();
      this.operationError = "";
      this.sync();
    },

    toggleHistory() {
      this.historyOpen = !this.historyOpen;
      this.closeChatMenu();
    },

    toggleChatMenu(id) {
      this.chatMenu = this.chatMenu === id ? null : id;
      this.chatAction = null;
    },

    closeChatMenu() {
      this.chatMenu = null;
      this.chatAction = null;
    },

    beginRename(chat) {
      this.renameDraft = chat.title;
      this.chatAction = "rename";
    },

    async applyChatAction(id) {
      const title = this.renameDraft.trim();
      if (this.running || this.historySaving || (this.chatAction === "rename" && !title)) return;
      this.historySaving = true;
      try {
        if (this.chatAction === "rename") {
          const record = records.get(id);
          await sessions.save({ ...record, title });
          record.title = title;
        } else {
          await sessions.remove(id);
          records.delete(id);
          if (active.id === id) {
            const next = this.conversations.find(chat => chat.id !== id);
            selectRecord(next ? records.get(next.id) : newSession());
            this.draft = "";
            this.operationError = "";
          }
        }
        this.closeChatMenu();
        this.sync();
      } finally {
        this.historySaving = false;
      }
    },

    exportChat() {
      const data = { title: active.title, messages: active.visible, compressions: active.compressions };
      const download = document.createElement("a");
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
      download.href = url;
      download.download = "icourse-chat-" + active.id.slice(0, 8) + ".json";
      download.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    },

    selectProvider() {
      const id = this.form.provider;
      this.form.apiKey = "";
      this.settingsNote = "";
      if (id === "custom") return;
      const preset = presets[id];
      this.form = { ...this.form, url: preset.url, model: preset.model, contextTokens: preset.contextTokens, apiKey: "" };
      this.settingsNote = "";
    },

    openSettings() {
      this.form = { ...this.config };
      this.settingsNote = "";
    },

    save() {
      try {
        const value = formConfig(this.form);
        saveConfig(value);
        this.config = value;
        this.form = { ...value };
        this.settingsNote = "问答设置已保存。";
        this.sync();
      } catch (error) {
        if (!(error instanceof ModelRequestError)) throw error;
        this.settingsNote = error.message;
      }
    },

    clearCredentials() {
      this.stop();
      this.config = { ...this.config, apiKey: "", rememberKey: false };
      this.form = { ...this.config };
      saveConfig(this.config);
    },

    async test() {
      this.testing = true;
      this.settingsNote = "";
      try {
        const value = formConfig(this.form);
        if (!ready(value)) throw new ModelRequestError("请先填写端点和模型。", "configuration");
        await testConnection(value, spec, AbortSignal.timeout(60000));
        this.settingsNote = "连接成功，模型已完成一次工具调用并收到返回结果。";
      } catch (error) {
        if (!(error instanceof ModelRequestError) && error.name !== "TimeoutError") throw error;
        this.settingsNote = error.name === "TimeoutError" ? "连接测试超时。" : error.message;
      } finally {
        this.testing = false;
      }
    },

    toolText({ name, args }) {
      if (name === "search_lecture") {
        const terms = args.keywords.map(quote).join(" ");
        return "Searching " + terms + " · " + args.modes.join("/") + (args.cursor ? " · next page" : "");
      }
      if (name === "read_lecture") {
        const cursor = readCursorLocation(args.cursor);
        const ids = args.lecture_ids ?? [cursor?.id];
        const labels = ids.filter(Boolean).map(id => window.ICS.db.lectureSource(id)?.lecture_title ?? id);
        const mode = args.mode ?? cursor?.mode ?? "summary";
        return "Reading " + mode + (labels.length ? " of " + labels.map(quote).join(", ") : "") +
          (args.cursor ? " · next page" : "");
      }
      if (name === "search_course") return "Searching courses for " + quote(args.query);
      if (name === "read_course") {
        const title = app.courses.find(course => course.course_id === args.course_id)?.title ?? args.course_id;
        return "Reading lecture list for " + quote(title) + (args.cursor ? " · next page" : "");
      }
      return "Listing courses" + (args.page ? " · page " + (args.page + 1) : "");
    },

    renderAnswer(text) {
      const findSource = app.view === "loading" ? () => undefined : window.ICS.db.lectureSource;
      return window.ICS.render.renderAnswer(text, findSource, citationNumbers);
    },

    openCitation(event) {
      const citation = event.target.closest("button.agent-citation");
      if (!citation) return;
      const { lectureId, mode } = citation.dataset;
      if (Object.hasOwn(citationViews, mode) && window.ICS.db.lectureSource(lectureId)) {
        app.openLecture(lectureId, citationViews[mode]);
        app._scrollToTop();
      }
    },

    fitInput(input) {
      input.style.height = "auto";
      const border = input.offsetHeight - input.clientHeight;
      input.style.height = (input.scrollHeight + border) + "px";
    },

    enter(event) {
      if (event.shiftKey || event.isComposing || event.keyCode === 229) return;
      event.preventDefault();
      if (!event.repeat) this.send();
    },

    async send(resume = false) {
      const text = resume ? "" : this.draft;
      if (this.running || this.historySaving || !this.configured || (!resume && !text.trim())) return;
      if (!resume) this.draft = "";
      this.running = true;
      this.operationError = "";
      this.notice = "";
      this.paused = false;
      this.closeChatMenu();
      controller = new AbortController();
      const dataset = [app.repoOwner, app.repoName, app.dataBranch].join("/");
      const page = this.desktop && !resume ? currentPage() ?? (app.view === "agent" ? lastPage : null) : null;
      const reader = window.ICS.db.agentReader();
      records.set(active.id, active);
      try {
        await runTurn({
          session: active, spec, config: { ...this.config }, reader, dataset,
          snapshot: dataset + "@" + (app.commitSha ?? "empty"), page, text, signal: controller.signal,
          onText: value => {
            this.liveText = value;
            this.answerStarted = Boolean(value.trim());
          },
          publish: async record => {
            if (!record.run && record.visible.at(-1)?.role === "assistant") {
              this.liveText = "";
              this.answerStarted = true;
            }
            this.sync();
            await sessions.save(record);
            if (this.messageList.at(-1)?.role === "tools") await delay();
          },
        });
      } catch (error) {
        console.error(error);
        if (!resume && active.visible.at(-1)?.text !== text && !active.run) this.draft = text;
        active.run = { status: "paused", detail: "本轮未完成，请检查浏览器控制台后重试。" };
        this.operationError = active.run.detail;
      } finally {
        this.liveText = "";
        controller = null;
        this.running = false;
        this.answerStarted = false;
        this.sync();
      }
    },

    stop() { controller?.abort(); },

    togglePane() {
      this.closed = !this.closed;
      paneSave(this);
    },

    beginResize(event) {
      drag = { x: event.clientX, moved: false };
      event.currentTarget.setPointerCapture(event.pointerId);
    },

    resize(event) {
      if (!drag) return;
      if (Math.abs(event.clientX - drag.x) < 4 && !drag.moved) return;
      drag.moved = true;
      const width = window.innerWidth - event.clientX;
      this.closed = width < 195;
      if (!this.closed) this.width = Math.min(maxPaneWidth(), Math.max(260, width));
    },

    endResize(event) {
      if (!drag) return;
      if (!drag.moved && event.type === "pointerup") this.togglePane();
      else paneSave(this);
      drag = null;
    },

    resizeBy(delta) {
      const next = Math.min(maxPaneWidth(), this.width) + delta;
      if (this.closed && delta > 0) this.closed = false;
      else if (next < 260) this.closed = true;
      else this.width = Math.min(maxPaneWidth(), next);
      paneSave(this);
    },
  };
  state.sync();
  return state;
}
