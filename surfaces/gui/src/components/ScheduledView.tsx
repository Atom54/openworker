import { useEffect, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import {
  createAutomation,
  deleteAutomation,
  getAutomation,
  getAutomations,
  getSettings,
  markAutomationSeen,
  announceAutomationsChanged,
  updateAutomation,
  type Automation,
  type AutomationRun,
} from "../api";
import { Icon } from "./Icon";
import { PanelHead } from "./IntegrationsView";
import { AutomationQuickstart } from "./AutomationQuickstart";
import { ScheduleFields, looksLikeCron } from "./Recurrence";

// Shared utility strings (the §28 page shell — mirrors IntegrationsView's constants).
const CARD = "rounded-xl2 border border-line bg-panel";

// Reasoning levels the backend accepts (SessionManager.THINKING_LEVELS).
const THINKING_LEVELS = ["none", "low", "medium", "high", "xhigh"];

/** The models list + labels the run-settings selects need, loaded once per view. */
export interface ModelChoices {
  models: string[];
  labels: Record<string, string>;
  appDefault: string;
}

/** Which models take a per-run reasoning level: the Responses API wires — native OpenAI
 *  (bare ids) and Azure AI Foundry. Anthropic configures thinking on the provider and the
 *  Chat Completions vendors reject the parameter, so the control is disabled for them
 *  rather than silently ignored. The provider router enforces this too (`_supported`);
 *  this is only what the form shows. */
function takesThinkingLevel(model: string): boolean {
  if (!model) return false;
  return !model.includes(":") || model.startsWith("azure:");
}

/** Model + reasoning-level pickers, shared by the create form and the detail editor so
 *  the two can't drift. `model: ""` / `thinking: ""` mean "follow the app default". */
function RunSettings({
  choices,
  model,
  thinking,
  onModel,
  onThinking,
}: {
  choices: ModelChoices | null;
  model: string;
  thinking: string;
  onModel: (v: string) => void;
  onThinking: (v: string) => void;
}) {
  if (!choices) return null;
  const label = (m: string) => choices.labels[m] || m;
  const effective = model || choices.appDefault;
  const openai = takesThinkingLevel(effective);
  return (
    <div className="tmpl-sched">
      <label className="tmpl-field">
        <span>Model</span>
        <select
          className="tmpl-input tmpl-select"
          value={model}
          data-testid="automation-model"
          onChange={(e) => onModel(e.target.value)}
        >
          <option value="">
            App default{choices.appDefault ? ` (${label(choices.appDefault)})` : ""}
          </option>
          {choices.models.map((m) => (
            <option key={m} value={m}>
              {label(m)}
            </option>
          ))}
        </select>
      </label>
      <label className="tmpl-field">
        <span>Thinking</span>
        <select
          className="tmpl-input tmpl-select"
          value={openai ? thinking : ""}
          disabled={!openai}
          data-testid="automation-thinking"
          title={
            openai
              ? "How hard the model reasons on each run."
              : `${label(effective)} keeps its own thinking setting — only OpenAI and Azure models take a level here.`
          }
          onChange={(e) => onThinking(e.target.value)}
        >
          <option value="">Model default</option>
          {THINKING_LEVELS.map((lvl) => (
            <option key={lvl} value={lvl}>
              {lvl[0].toUpperCase() + lvl.slice(1)}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

const fmt = (t: number | null) =>
  t ? new Date(t * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";

// The §28 page shell: full-bleed main, centered ≤4xl column — same as Connectors/Activity/Inbox.
function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="flex-1 min-w-0 flex bg-paper">
      <div className="flex-1 min-w-0 overflow-y-auto hairline-scroll">
        <div className="max-w-4xl mx-auto px-7 py-6">{children}</div>
      </div>
    </main>
  );
}

interface Props {
  // `task` gives the opened run session its context (banner + "Back to runs"; owner ask 2026-07-04).
  onOpenRun: (
    sessionId: string,
    workspace: string,
    agent: string,
    task?: { id: string; title: string },
  ) => void;
  onRunNow: (taskId: string, title?: string) => void;
  // Open directly on a task's detail (set by the run banner's "Back to runs").
  initialOpenId?: string | null;
}

export function ScheduledView({ onOpenRun, onRunNow, initialOpenId }: Props) {
  const { t } = useTranslation();
  const [tasks, setTasks] = useState<Automation[]>([]);
  const [openId, setOpenId] = useState<string | null>(initialOpenId ?? null);
  const [showForm, setShowForm] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  // The sidebar's Scheduled band can retarget an ALREADY-open Automations surface —
  // initial state alone would ignore the change (UX-023).
  useEffect(() => {
    if (initialOpenId) setOpenId(initialOpenId);
  }, [initialOpenId]);

  const refresh = () => getAutomations().then(setTasks).catch(() => setTasks([]));
  useEffect(() => {
    refresh();
    const h = setInterval(refresh, 5000);
    return () => clearInterval(h);
  }, []);

  // The curated model list feeds both the create form and the detail editor. Loaded once:
  // it only changes from Settings, which leaves this surface anyway.
  const [choices, setChoices] = useState<ModelChoices | null>(null);
  useEffect(() => {
    getSettings()
      .then((s) =>
        setChoices({
          models: s.models || [],
          labels: s.model_labels || {},
          appDefault: s.model || "",
        }),
      )
      .catch(() => {});
  }, []);

  // Create from a payload, refresh the list, and open the new task's detail. `permissions`
  // rides through for quickstart recipes (§25 write grants).
  const create = async (payload: {
    title: string;
    instructions: string;
    cron?: string;
    model?: string;
    thinking?: string;
    permissions?: { tool: string; target: string; access: "read" | "write" }[];
  }) => {
    setBusy(payload.title);
    try {
      const res = await createAutomation(payload);
      announceAutomationsChanged(); // new entry shows in the sidebar band right away
      await refresh();
      if (res.ok && res.task) {
        setShowForm(false);
        setOpenId(res.task.id);
      } else if (res.error) {
        alert(res.error);
      }
    } finally {
      setBusy(null);
    }
  };

  if (openId) {
    return (
      <TaskDetail
        id={openId}
        choices={choices}
        onBack={() => { setOpenId(null); refresh(); }}
        onOpenRun={onOpenRun}
        onRunNow={onRunNow}
      />
    );
  }

  const empty = tasks.length === 0;

  return (
    <Shell>
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <PanelHead title={t("automations.title")} sub={t("automations.sub")} />
        </div>
        <button
          className="text-[13px] px-3 py-1.5 rounded-lg border border-lineStrong bg-panel hover:border-accent hover:text-accent shrink-0"
          onClick={() => setShowForm((v) => !v)}
        >
          {t("automations.new_btn")}
        </button>
      </div>

      <div className="text-[12px] text-faint flex gap-1.5 mb-4">
        <span aria-hidden>ⓘ</span>
        <span>{t("automations.server_hint")}</span>
      </div>

      {showForm && (
        <NewAutomationForm
          busy={busy !== null}
          choices={choices}
          onCancel={() => setShowForm(false)}
          onCreate={create}
        />
      )}

      {/* The quickstart (§29): ONE template system — role recipes + generic templates, each
          card with §27 connector dots; picking one expands the configure card. */}
      {(empty || showForm) && <AutomationQuickstart busy={busy !== null} onCreate={create} />}

      {empty ? (
        !showForm && (
          <div className={CARD + " p-4 text-[13px] text-muted"}>
            <Trans
              i18nKey="automations.empty_state"
              components={{ strong: <strong /> }}
            />
          </div>
        )
      ) : (
        <div className="flex flex-col gap-2.5">
          {tasks.map((task) => (
            <div
              className={CARD + " sched-card px-4 py-3 cursor-pointer hover:border-lineStrong transition-colors"}
              key={task.id}
              onClick={() => setOpenId(task.id)}
            >
              <div className="flex items-center justify-between gap-2.5 mb-1">
                <span className="text-[13px] font-semibold truncate">{task.title}</span>
                <button
                  className="sched-card-del"
                  title={t("automations.delete_title")}
                  aria-label={t("automations.delete_aria", { title: task.title })}
                  onClick={async (e) => {
                    e.stopPropagation();
                    await deleteAutomation(task.id);
                    refresh();
                  }}
                >
                  <Icon name="trash" size={14} />
                </button>
              </div>
              <div className="flex items-center gap-1.5 text-[12px] text-muted">
                <Icon name="clock" size={13} className="text-faint shrink-0" />
                {task.enabled ? task.schedule : t("automations.paused")} · {t("automations.next", { time: fmt(task.next_run) })} · {t("automations.run_count", { count: task.run_count })}
                {task.last_status ? ` · ${t("automations.last", { status: task.last_status })}` : ""}
              </div>
            </div>
          ))}
        </div>
      )}
    </Shell>
  );
}

function NewAutomationForm({
  busy,
  onCancel,
  onCreate,
  choices,
}: {
  busy: boolean;
  onCancel: () => void;
  onCreate: (p: {
    title: string;
    instructions: string;
    cron?: string;
    model?: string;
    thinking?: string;
  }) => void;
  choices: ModelChoices | null;
}) {
  const { t } = useTranslation();
  const [title, setTitle] = useState("");
  const [instructions, setInstructions] = useState("");
  const [cron, setCron] = useState("0 9 * * *");
  const [model, setModel] = useState("");
  const [thinking, setThinking] = useState("");

  const valid = title.trim() && instructions.trim() && looksLikeCron(cron);

  return (
    <div className={CARD + " tmpl-form p-4 mb-4"}>
      <div className="text-[11px] uppercase tracking-[0.05em] text-faint mb-2.5">
        {t("automations.new_automation")}
      </div>
      <input
        className="tmpl-input"
        placeholder={t("automations.title_placeholder")}
        value={title}
        onChange={(e) => setTitle(e.target.value)}
      />
      <textarea
        className="tmpl-input tmpl-textarea"
        placeholder={t("automations.instructions_placeholder")}
        value={instructions}
        onChange={(e) => setInstructions(e.target.value)}
      />
      <ScheduleFields onCron={setCron} />
      <RunSettings
        choices={choices}
        model={model}
        thinking={thinking}
        onModel={setModel}
        onThinking={setThinking}
      />
      <div className="tmpl-form-actions">
        <button
          className="btn-primary sm"
          disabled={!valid || busy}
          onClick={() =>
            onCreate({
              title: title.trim(),
              instructions: instructions.trim(),
              cron,
              model,
              thinking,
            })
          }
        >
          {busy ? t("automations.creating") : t("automations.create_btn")}
        </button>
        <button className="link" onClick={onCancel}>{t("automations.cancel")}</button>
      </div>
    </div>
  );
}

function TaskDetail({
  id,
  choices,
  onBack,
  onOpenRun,
  onRunNow,
}: {
  id: string;
  choices: ModelChoices | null;
  onBack: () => void;
  onOpenRun: (
    sessionId: string,
    workspace: string,
    agent: string,
    task?: { id: string; title: string },
  ) => void;
  onRunNow: (taskId: string, title?: string) => void;
}) {
  const { t: tt } = useTranslation();
  const [task, setTask] = useState<Automation | null>(null);
  const [runs, setRuns] = useState<AutomationRun[]>([]);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [instructions, setInstructions] = useState("");
  const [cron, setCron] = useState("0 9 * * *");
  const [model, setModel] = useState("");
  const [thinking, setThinking] = useState("");
  const [saving, setSaving] = useState(false);
  // The server has the last word on a cron (croniter parses what the shape check can't).
  const [saveError, setSaveError] = useState<string | null>(null);

  // The seen mark AS OF opening — the "new" pills compare against this frozen value
  // while mark-seen advances the stored one (badge clears; highlights survive).
  const [seenMark, setSeenMark] = useState<number | null>(null);

  const refresh = () =>
    getAutomation(id)
      .then((d) => {
        if (!d.task) {
          // Deleted (or a stale reopen target): "Loading…" forever is a trap —
          // fall back to the overview (owner-hit 2026-07-20).
          onBack();
          return;
        }
        setTask(d.task);
        setRuns(d.runs || []);
        setSeenMark((cur) => (cur === null ? d.task?.seen_runs_at ?? 0 : cur));
      })
      .catch(() => {});
  useEffect(() => {
    setSeenMark(null);
    refresh();
    // Opening the detail IS reading it: advance the seen mark and nudge the
    // sidebar so the badge clears immediately (UX-023).
    markAutomationSeen(id)
      .then(() => announceAutomationsChanged())
      .catch(() => {});
  }, [id]);

  if (!task)
    return (
      <Shell>
        <div className="text-[13px] text-muted">{tt("automations.loading")}</div>
      </Shell>
    );

  const startEdit = () => {
    setTitle(task.title);
    setInstructions(task.instructions);
    setModel(task.model || "");
    setThinking(task.thinking || "");
    setSaveError(null);
    setEditing(true);
  };
  const saveEdit = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const res = await updateAutomation(id, {
        title: title.trim(),
        instructions: instructions.trim(),
        cron,
        model,
        thinking,
      });
      if (res && res.ok === false) {
        // A rejected cron must not close the editor — the typed schedule would be lost.
        setSaveError(res.error || "could not save");
        return;
      }
      await refresh();
      setEditing(false);
    } finally {
      setSaving(false);
    }
  };
  const toggle = async () => {
    await updateAutomation(id, { enabled: !task.enabled });
    refresh();
  };
  // Per-automation silence: one that reliably works shouldn't announce itself every run.
  // Failures still reach the Scheduled badge; this only mutes the notification.
  const toggleNotify = async () => {
    await updateAutomation(id, { notify_on_completion: !task.notify_on_completion });
    refresh();
  };
  const remove = async () => {
    await deleteAutomation(id);
    announceAutomationsChanged(); // the sidebar band must not wait out its poll
    onBack();
  };

  return (
    <Shell>
      <button className="text-[13px] text-muted hover:text-ink mb-3" onClick={onBack}>
        {tt("automations.back_to_automations")}
      </button>
      <div className="sched-detail">
        <div className="sched-detail-head">
          {editing ? (
            <input
              className="tmpl-input sched-edit-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder={tt("automations.title_label")}
            />
          ) : (
            <h2 className="text-[20px] font-semibold tracking-tight">{task.title}</h2>
          )}
          <div className="sched-actions">
            {editing ? (
              <>
                <button
                  className="btn-primary sm"
                  disabled={saving || !title.trim() || !instructions.trim() || !looksLikeCron(cron)}
                  onClick={saveEdit}
                >
                  {saving ? tt("automations.saving") : tt("automations.save")}
                </button>
                <button className="link" onClick={() => setEditing(false)}>{tt("automations.cancel")}</button>
              </>
            ) : (
              <>
                <button className="btn-primary sm" onClick={() => onRunNow(id, task.title)}>
                  {tt("automations.run_now")}
                </button>
                <button className="btn sm" onClick={startEdit}>{tt("automations.edit")}</button>
                <button className="btn sm danger-btn" onClick={remove}>
                  <Icon name="trash" size={14} /> {tt("automations.delete")}
                </button>
              </>
            )}
          </div>
        </div>

        {editing ? (
          <>
            <div className="sched-edit-sched">
              <ScheduleFields initial={task.schedule_raw?.cron} onCron={setCron} />
            </div>
            {saveError && <div className="mcp-error">{saveError}</div>}
            <RunSettings
              choices={choices}
              model={model}
              thinking={thinking}
              onModel={setModel}
              onThinking={setThinking}
            />
          </>
        ) : (
          <div className="conn-meta">
            <label className="switch">
              <input
                type="checkbox"
                data-testid="automation-enabled"
                checked={task.enabled}
                onChange={toggle}
              />
              <span className="slider" />
            </label>{" "}
            {task.enabled ? tt("automations.active_next", { time: fmt(task.next_run) }) : tt("automations.paused")} · {task.schedule}
            {task.model && (
              <> · {choices?.labels[task.model] || task.model}</>
            )}
            {task.thinking && <> · {tt("automations.thinking_suffix", { level: task.thinking })}</>}
            <div className="mt-2">
              <label className="switch">
                <input
                  type="checkbox"
                  data-testid="notify-on-completion"
                  checked={task.notify_on_completion !== false}
                  onChange={toggleNotify}
                />
                <span className="slider" />
              </label>{" "}
              {task.notify_on_completion !== false
                ? tt("automations.notify_on")
                : tt("automations.notify_off")}
            </div>
          </div>
        )}

        <div className="sa-sub">{tt("automations.instructions_label")}</div>
        {editing ? (
          <textarea
            className="tmpl-input tmpl-textarea sched-edit-instr"
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
          />
        ) : (
          <div className="sched-instructions">{task.instructions}</div>
        )}

        {(task.always_allowed || []).length > 0 && (
          <>
            <div className="sa-sub">{tt("automations.allowed_without_asking")}</div>
            <div className="dim" style={{ marginBottom: 8, fontSize: 12.5 }}>
              {tt("automations.allowed_desc")}
            </div>
            <div className="sched-grants" data-testid="task-grants">
              {(task.always_allowed || []).map((rule) => (
                <div className="sched-grant" key={rule.entry}>
                  <span className="sched-grant-rule">
                    <code>{rule.tool}</code>
                    {rule.target && <span className="sched-grant-target"> → {rule.target}</span>}
                  </span>
                  <button
                    className="link"
                    title={tt("automations.revoke_title")}
                    onClick={async () => {
                      await updateAutomation(id, { revoke: rule.entry });
                      refresh();
                    }}
                  >
                    {tt("automations.revoke")}
                  </button>
                </div>
              ))}
            </div>
          </>
        )}

        <div className="sa-sub">{tt("automations.runs_label")}</div>
        <div className="dim" style={{ marginBottom: 8, fontSize: 12.5 }}>
          {tt("automations.runs_desc")}
        </div>
        {runs.length === 0 && <div className="dim">{tt("automations.no_runs")}</div>}
        {runs.map((r) => (
          <div
            className="sched-run open"
            key={r.run_id}
            onClick={() =>
              r.session_id &&
              onOpenRun(r.session_id, task.workspace, task.agent, {
                id: task.id,
                title: task.title,
              })
            }
            title={tt("automations.open_run")}
          >
            <div className="sched-run-row">
              <span>
                {seenMark !== null && r.started_at > seenMark && (
                  <span className="run-new-pill" data-testid="run-new">{tt("automations.new_pill")}</span>
                )}
                {fmt(r.started_at)} · <span className={"run-" + r.status}>{r.status}</span> · {r.trigger}
                {r.artifacts.length > 0 && <span className="dim"> · {tt("automations.file_count", { count: r.artifacts.length })}</span>}
              </span>
              <span className="sched-run-go" aria-hidden>
                {tt("automations.open_go")}
              </span>
            </div>
            {r.result_text && <div className="sched-run-peek">{r.result_text}</div>}
            {r.error && <div className="mcp-error">{r.error}</div>}
          </div>
        ))}
      </div>
    </Shell>
  );
}
