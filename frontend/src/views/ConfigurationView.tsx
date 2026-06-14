import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Settings, Save, Loader2, AlertCircle, Power, OctagonX, Eraser, CheckCircle2,
  Scale, FileWarning, Zap, Radar,
} from "lucide-react";
import {
  api, type AgentConfig, type AutoTradeStatus, type DayTraderMode,
  type DayTraderStatus, type DayTraderScanResp,
} from "../lib/api";
import { cn, fmtUSD, fmtPct } from "../lib/cn";
import { haptic } from "../lib/native";

/**
 * Configuration view — single source of truth for autonomous-trader settings.
 *
 * Layout:
 *   1. Live status card (enabled? halted? P&L today? open positions?)
 *   2. Big toggles + emergency actions (Halt / Resume / Flatten All)
 *   3. Numeric knobs (budget, risk %, daily loss limit, max concurrent)
 *   4. Schedule (entry window, EOD flatten time)
 *   5. Policy toggles (margin allowed, notify on trade)
 *
 * Settings save individually via POST /api/config — no "are you sure" because
 * none of them place trades. The two destructive buttons (Halt, Flatten All)
 * have explicit confirmation.
 */
export function ConfigurationView() {
  const qc = useQueryClient();
  const cfgQ = useQuery<AgentConfig>({
    queryKey: ["agent-config"], queryFn: api.getConfig,
  });
  const status = useQuery<AutoTradeStatus>({
    queryKey: ["auto-trade-status"], queryFn: api.autoTradeStatus,
    refetchInterval: 15_000,
  });

  // Local draft state so the user can edit several fields before "Save".
  const [draft, setDraft] = useState<AgentConfig | null>(null);
  useEffect(() => {
    if (cfgQ.data && !draft) setDraft(cfgQ.data);
  }, [cfgQ.data, draft]);

  const save = useMutation({
    mutationFn: (patch: Partial<AgentConfig>) => api.updateConfig(patch),
    onSuccess: () => {
      haptic.success();
      qc.invalidateQueries({ queryKey: ["agent-config"] });
      qc.invalidateQueries({ queryKey: ["auto-trade-status"] });
    },
    onError: () => haptic.error(),
  });
  const halt = useMutation({
    mutationFn: () => api.autoTradeHalt("manual via Configuration"),
    onSuccess: () => qc.invalidateQueries(),
  });
  const resume = useMutation({
    mutationFn: () => api.autoTradeResume(),
    onSuccess: () => qc.invalidateQueries(),
  });
  const flatten = useMutation({
    mutationFn: () => api.flattenAll(),
    onSuccess: () => { haptic.success(); qc.invalidateQueries(); },
    onError:   () => haptic.error(),
  });

  if (!draft || !cfgQ.data) {
    return <div className="card card-tight">Loading configuration…</div>;
  }

  const s = status.data;
  const enabled = !!cfgQ.data.auto_trade_enabled;
  const halted = !!cfgQ.data.halted_at;
  const dirty = JSON.stringify(draft) !== JSON.stringify(cfgQ.data);

  const setField = <K extends keyof AgentConfig>(k: K, v: AgentConfig[K]) =>
    setDraft({ ...draft, [k]: v });

  return (
    <div className="max-w-3xl mx-auto space-y-3">
      {/* Header */}
      <div className="flex items-center gap-2">
        <Settings className="w-4 h-4 text-accent" />
        <h2 className="text-base font-semibold">Configuration</h2>
        <span className="text-xxs opacity-50 ml-auto">
          last updated {new Date(cfgQ.data.updated_at * 1000).toLocaleString()}
        </span>
      </div>

      {/* Live status card */}
      <div className={cn(
        "card p-3 space-y-2",
        halted && "border-sell/40 bg-sell/5",
        !halted && enabled && "border-buy/40 bg-buy/5",
      )}>
        <div className="flex items-center gap-2 text-sm">
          {halted ? (
            <><OctagonX className="w-4 h-4 text-sell" />
              <span className="font-semibold text-sell">HALTED</span>
              <span className="opacity-70 text-xs">— {cfgQ.data.halt_reason}</span></>
          ) : enabled ? (
            <><CheckCircle2 className="w-4 h-4 text-buy" />
              <span className="font-semibold text-buy">AUTO-TRADE ENABLED</span></>
          ) : (
            <><Power className="w-4 h-4 opacity-50" />
              <span className="opacity-70">Auto-trade is OFF</span></>
          )}
        </div>
        {s && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xxs">
            <Stat label="Today's P&L"
                  value={`${s.todays_pl >= 0 ? "+" : ""}${fmtUSD(s.todays_pl)}`}
                  sub={fmtPct(s.todays_pl_pct)}
                  tone={s.todays_pl >= 0 ? "buy" : "sell"} />
            <Stat label="Equity" value={fmtUSD(s.equity)}
                  sub={s.on_margin ? "on margin" : "cash-only"} />
            <Stat label="Open positions" value={`${s.open_positions} / ${draft.max_concurrent}`}
                  sub="vs cap" />
            <Stat label="Today's trades" value={`${s.today_buys} BUY · ${s.today_sells} SELL`} />
          </div>
        )}
      </div>

      {/* Big toggles + emergency actions */}
      <div className="card card-tight space-y-3">
        <ToggleRow
          label="Auto-trade enabled"
          help="When ON, BUY crossings on watchlist tickers are executed directly via Alpaca (no Telegram approval). All risk guards still apply."
          checked={draft.auto_trade_enabled}
          onChange={(v) => { setField("auto_trade_enabled", v); save.mutate({ auto_trade_enabled: v }); }}
          disabled={halted}
        />
        {halted && (
          <div className="text-xxs text-sell flex items-start gap-1.5">
            <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            <span>
              Trading is halted ({cfgQ.data.halt_reason}). Resume before re-enabling auto-trade.
            </span>
          </div>
        )}

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 pt-1">
          {halted ? (
            <button
              onClick={() => { haptic.medium(); resume.mutate(); }}
              disabled={resume.isPending}
              className="btn btn-primary"
            >
              {resume.isPending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Power className="w-3.5 h-3.5" />}
              Resume (clear halt)
            </button>
          ) : (
            <button
              onClick={() => {
                haptic.heavy();
                if (window.confirm("Halt auto-trading? New entries will be blocked until resumed.")) {
                  halt.mutate();
                }
              }}
              disabled={halt.isPending || !enabled}
              className="btn btn-sell"
            >
              {halt.isPending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <OctagonX className="w-3.5 h-3.5" />}
              Halt trading
            </button>
          )}

          <button
            onClick={() => {
              haptic.heavy();
              if (window.confirm(
                "Sell ALL open positions immediately?\n\n" +
                "If the market is closed, Alpaca queues these as market SELLs " +
                "for the next regular session open.\n\n" +
                "Open SL/TP bracket orders will also be cancelled."
              )) {
                flatten.mutate();
              }
            }}
            disabled={flatten.isPending}
            className="btn btn-sell sm:col-span-2"
          >
            {flatten.isPending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Eraser className="w-3.5 h-3.5" />}
            Clean slate · sell all positions
          </button>
        </div>

        {flatten.data && (
          <div className="text-xxs">
            {flatten.data.closed_count > 0 && <>✅ Closed {flatten.data.closed_count} positions. </>}
            {flatten.data.queued_for_open > 0 && (
              <>📋 Queued {flatten.data.queued_for_open} Market-on-Open SELLs for next session.</>
            )}
            {flatten.data.errors.length > 0 && (
              <div className="text-amber-500 mt-1">{flatten.data.errors[0]}</div>
            )}
          </div>
        )}
        {cfgQ.data.pending_open_flatten && (
          <div className="text-xxs text-amber-500 flex items-start gap-1.5">
            <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            <span>
              Flatten queued for next market open — Alpaca paper's protective
              brackets release at the open, then the EOD daemon retries the
              flatten automatically.
            </span>
          </div>
        )}
      </div>

      {/* Capital & risk */}
      <Section title="Capital & risk">
        <NumRow
          label="Budget (USD)"
          help="Total capital the agent may deploy. Used for per-trade sizing and the daily-loss-% basis."
          value={draft.budget}
          step={500} min={0}
          onChange={(v) => setField("budget", v)}
          format={fmtUSD}
        />
        <NumRow
          label="Risk per trade (%)"
          help="Max loss-at-stop per single trade as % of budget. 1% on $10k = $100."
          value={draft.risk_per_trade_pct}
          step={0.1} min={0.1} max={5}
          onChange={(v) => setField("risk_per_trade_pct", v)}
          format={(v) => `${v}%`}
        />
        <NumRow
          label="Daily loss limit (%)"
          help="If today's drawdown exceeds this % of equity, auto-trade halts for the day. 2% on $10k = $200."
          value={draft.daily_loss_limit_pct}
          step={0.5} min={0.5} max={10}
          onChange={(v) => setField("daily_loss_limit_pct", v)}
          format={(v) => `${v}%`}
        />
        <NumRow
          label="Max concurrent positions"
          help="Cap on simultaneous open positions. Also caps per-position $ to budget / cap."
          value={draft.max_concurrent}
          step={1} min={1} max={20}
          onChange={(v) => setField("max_concurrent", Math.round(v))}
          format={(v) => String(v)}
        />
      </Section>

      {/* Schedule */}
      <Section title="Day-trading schedule (ET)">
        <TimeRow
          label="Entry window start"
          help="Earliest entry time. Skips the opening 5-min volatility (default 09:35)."
          value={draft.entry_window_start}
          onChange={(v) => setField("entry_window_start", v)}
        />
        <TimeRow
          label="Entry window end"
          help="Latest entry time. After this, no new BUYs (default 15:00, an hour before close)."
          value={draft.entry_window_end}
          onChange={(v) => setField("entry_window_end", v)}
        />
        <TimeRow
          label="EOD flatten time"
          help="Auto-flatten everything at this ET time so positions don't carry overnight (default 15:50)."
          value={draft.eod_flatten_time}
          onChange={(v) => setField("eod_flatten_time", v)}
        />
      </Section>

      {/* Policy toggles */}
      <Section title="Policy">
        <ToggleRow
          label="Allow margin"
          help="When OFF (recommended for day trading), entries refuse if cash ≤ 0 — no new debt."
          checked={draft.margin_allowed}
          onChange={(v) => setField("margin_allowed", v)}
        />
        <ToggleRow
          label="Telegram notify on every trade"
          help="Each AUTO entry sends a notification to your Telegram chat. Audit trail."
          checked={draft.notify_on_trade}
          onChange={(v) => setField("notify_on_trade", v)}
        />
      </Section>

      {/* Day-trader (Session 8) — parallel intraday brain.
          Mode flips commit immediately via /api/day-trader/mode (no Save dance,
          because mode = "live operator action"). Knobs use the regular draft +
          Save flow. The scanner button populates the universe right now. */}
      <DayTraderPanel
        draft={draft}
        setField={setField}
        liveCfg={cfgQ.data}
      />

      {/* Tax mode (§475 election) — informational, mostly read-only.
          Separates "is the env flag on" (deploy concern) from "have you
          actually filed the election with the IRS" (user concern). Goes
          red when those disagree so the flag can't be flipped on
          accidentally without acknowledging tax reality. */}
      <Section475Panel
        envActive={cfgQ.data.section_475_env_active}
        electionFiled={draft.section_475_election_filed}
        onChangeElectionFiled={(v) => {
          setField("section_475_election_filed", v);
          save.mutate({ section_475_election_filed: v });
        }}
      />

      {/* Save bar — only shown when there are unsaved edits */}
      {dirty && (
        <div className="sticky bottom-3 z-30 card p-3 flex items-center gap-3 shadow-lg
                        border-accent/40 bg-white dark:bg-zinc-950">
          <div className="text-xs">You have unsaved changes.</div>
          <div className="flex-1" />
          <button
            onClick={() => setDraft(cfgQ.data)}
            className="btn btn-ghost"
          >Discard</button>
          <button
            onClick={() => { haptic.medium(); save.mutate(draft); }}
            disabled={save.isPending}
            className="btn btn-primary"
          >
            {save.isPending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
            Save
          </button>
        </div>
      )}
    </div>
  );
}

// ────── helpers ──────

/**
 * Tax-mode panel — §475(f) "trader mark-to-market" election.
 *
 * Why the two flags are separate:
 *   • `NUROQ_SECTION_475` is a DEPLOY-time env var. It tells NuroQ's
 *     wash-sale guard to short-circuit (re-entries on losses are allowed,
 *     because §475 traders don't have wash-sale rules). Set by whoever
 *     deploys the cloud / runs local, not by the UI user.
 *   • `section_475_election_filed` is a USER acknowledgement that the
 *     actual §475(f) election has been filed with the IRS (election
 *     statement + Form 3115). NOT something NuroQ can verify — only the
 *     user knows.
 *
 * When they disagree (env ON but election_filed OFF), NuroQ is letting
 * the user re-enter on losses freely while the IRS will still apply
 * wash-sale rules and disallow those losses on the 1099-B. Worst-case
 * outcome — net-positive tax bill on a net-losing year. Big red warning.
 */
function Section475Panel({
  envActive, electionFiled, onChangeElectionFiled,
}: {
  envActive: boolean;
  electionFiled: boolean;
  onChangeElectionFiled: (v: boolean) => void;
}) {
  const mismatch = envActive && !electionFiled;
  const aligned  = envActive === electionFiled;

  return (
    <div className="card card-tight space-y-3">
      <div className="text-xxs uppercase tracking-wide opacity-50 flex items-center gap-1.5">
        <Scale className="w-3 h-3" />
        Tax mode (§475(f) election)
      </div>

      {/* Status banner — color reflects (env, filed) alignment */}
      <div className={cn(
        "flex items-start gap-2 text-xs p-2 rounded border",
        mismatch && "border-sell/40 bg-sell/10 text-sell",
        !mismatch && envActive && aligned && "border-buy/40 bg-buy/10",
        !envActive && "border-zinc-300 dark:border-zinc-700 bg-zinc-50 dark:bg-zinc-900",
      )}>
        {mismatch
          ? <FileWarning className="w-4 h-4 shrink-0 mt-0.5" />
          : envActive
            ? <CheckCircle2 className="w-4 h-4 shrink-0 mt-0.5" />
            : <AlertCircle className="w-4 h-4 shrink-0 mt-0.5 opacity-60" />}
        <div className="leading-relaxed">
          {mismatch && (
            <>
              <div className="font-semibold mb-1">
                MISMATCH — §475 software flag is ON but election is NOT marked filed.
              </div>
              <div className="opacity-90">
                NuroQ's wash-sale guard is disabled (re-entries on losses allowed),
                but if the §475(f) election isn't actually on file with the IRS,
                wash-sale rules will still apply on your 1099-B and the losses
                will be disallowed. Either file the election (then mark it below)
                or set <code className="font-mono">NUROQ_SECTION_475=0</code> on the deploy.
              </div>
            </>
          )}
          {!mismatch && envActive && (
            <div>
              §475(f) elected — full ordinary loss deduction, no wash-sale rules.
              Year-end positions get marked to market.
            </div>
          )}
          {!envActive && (
            <div>
              Default investor regime — wash-sale rules apply (30-day loss
              re-entry disallowed), capital losses capped at $3,000/yr against
              ordinary income.
            </div>
          )}
        </div>
      </div>

      <div className="space-y-2">
        <Row
          label="Software flag (NUROQ_SECTION_475)"
          help="Deploy-time env var. Controls whether NuroQ's wash-sale advisory short-circuits. NOT editable from the UI — change on the deploy."
          value={envActive ? "ON" : "OFF"}
          valueTone={envActive ? "buy" : undefined}
        />
        <ToggleRow
          label="§475(f) election is on file with the IRS"
          help="Check ONLY after you've actually filed the election (statement + Form 3115) with your CPA. Election deadlines are strict: individuals must file by April 15 of the tax year; new entities have ~75 days from formation. The 2026 individual window is closed."
          checked={electionFiled}
          onChange={onChangeElectionFiled}
        />
      </div>

      {/* Reference card — the 4-row "what this actually does" table */}
      <details className="text-xxs opacity-80">
        <summary className="cursor-pointer font-semibold opacity-100">
          What §475(f) changes (click to expand)
        </summary>
        <table className="w-full mt-2 font-mono">
          <thead>
            <tr className="opacity-60">
              <th className="text-left py-1">Rule</th>
              <th className="text-left py-1">Default (investor)</th>
              <th className="text-left py-1">§475(f) trader</th>
            </tr>
          </thead>
          <tbody className="opacity-90">
            <tr><td className="py-0.5">Wash sales</td><td>Disallowed</td><td className="text-buy">Don't apply</td></tr>
            <tr><td className="py-0.5">Loss deduction</td><td>$3,000/yr cap</td><td className="text-buy">No cap (ordinary loss)</td></tr>
            <tr><td className="py-0.5">Gain character</td><td>Capital</td><td>Ordinary</td></tr>
            <tr><td className="py-0.5">Year-end open positions</td><td>Held</td><td>Marked to market</td></tr>
            <tr><td className="py-0.5">Reported on</td><td>Sched D / 8949</td><td>Form 4797 + Form 3115</td></tr>
          </tbody>
        </table>
        <div className="mt-2 leading-relaxed">
          You must also qualify as a <i>trader</i> by IRS criteria — substantial
          activity (often 4+ days/week, hundreds of trades/year), short holds,
          profit motive from short-term swings, primary livelihood. Side
          trading on top of a W-2 typically won't qualify. A CPA familiar with
          trader taxes is strongly recommended before electing.
        </div>
      </details>
    </div>
  );
}

function Row({
  label, help, value, valueTone,
}: { label: string; help?: string; value: string; valueTone?: "buy" | "sell" }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="flex-1 min-w-0">
        <div className="text-xs font-medium">{label}</div>
        {help && <div className="text-xxs opacity-60 leading-relaxed">{help}</div>}
      </div>
      <div className={cn(
        "font-mono text-xs font-semibold",
        valueTone === "buy" && "text-buy",
        valueTone === "sell" && "text-sell",
      )}>{value}</div>
    </div>
  );
}


// ────────────────────────────────────────────────────────────────────────
// Day-trader panel (Session 8)
// ────────────────────────────────────────────────────────────────────────
//
// Three columns of operator surface:
//   1. Live status: mode pill, fires today, open positions, universe size
//   2. Mode promotion: 4-button row (disabled / shadow / approve / auto).
//      Commits IMMEDIATELY — mode is high-stakes operator state, not a draft.
//   3. Universe + scan button + numeric knobs (draft → Save)
//
// Mode colors mirror real-world risk:
//   disabled → grey  · shadow → blue  · approve → amber  · auto → red

const MODE_OPTIONS: { mode: DayTraderMode; label: string; help: string; cls: string }[] = [
  { mode: "disabled", label: "Disabled", help: "Engine inactive.",
    cls: "bg-zinc-200 dark:bg-zinc-800 text-zinc-700 dark:text-zinc-300" },
  { mode: "shadow",   label: "Shadow",   help: "Logs would-be fires. Places NO orders.",
    cls: "bg-blue-100 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300" },
  { mode: "approve",  label: "Approve",  help: "Telegram approval flow. NO auto orders.",
    cls: "bg-amber-100 dark:bg-amber-950/40 text-amber-700 dark:text-amber-300" },
  { mode: "auto",     label: "AUTO",     help: "Risk-gated bracket orders placed automatically.",
    cls: "bg-red-100 dark:bg-red-950/40 text-red-700 dark:text-red-300" },
];

function DayTraderPanel({
  draft, setField, liveCfg,
}: {
  draft: AgentConfig;
  setField: <K extends keyof AgentConfig>(k: K, v: AgentConfig[K]) => void;
  liveCfg: AgentConfig;
}) {
  const qc = useQueryClient();
  const statusQ = useQuery<DayTraderStatus>({
    queryKey: ["dt-status"], queryFn: api.dayTraderStatus,
    refetchInterval: 15_000,
  });
  const setMode = useMutation({
    mutationFn: (m: DayTraderMode) => api.dayTraderSetMode(m),
    onSuccess: () => {
      haptic.success();
      qc.invalidateQueries({ queryKey: ["agent-config"] });
      qc.invalidateQueries({ queryKey: ["dt-status"] });
    },
    onError: () => haptic.error(),
  });
  const scan = useMutation<DayTraderScanResp>({
    mutationFn: () => api.dayTraderScan(),
    onSuccess: (r) => {
      haptic.success();
      // dt_universe is updated server-side — refresh config so the input reflects it.
      qc.invalidateQueries({ queryKey: ["agent-config"] });
      qc.invalidateQueries({ queryKey: ["dt-status"] });
      // Also stuff the new universe into the draft so the user sees it immediately.
      setField("dt_universe", r.universe);
    },
    onError: () => haptic.error(),
  });

  const liveMode = liveCfg.dt_mode;
  const s = statusQ.data;
  const showAutoWarn = liveMode === "auto";

  return (
    <div className="card card-tight space-y-3">
      <div className="text-xxs uppercase tracking-wide opacity-50 flex items-center gap-1.5">
        <Zap className="w-3 h-3" />
        Day-trader (intraday brain — ORB-5 + VWAP)
      </div>

      {/* Live status row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
        <Stat
          label="Mode"
          value={(s?.mode ?? liveMode ?? "disabled").toUpperCase()}
          tone={liveMode === "auto" ? "sell" : liveMode === "disabled" ? undefined : "buy"}
        />
        <Stat label="Fires today" value={String(s?.fires_today ?? 0)} />
        <Stat label="Open DT positions" value={String(s?.open_position_count ?? 0)} />
        <Stat label="Universe size" value={String(s?.universe_size ?? 0)} />
      </div>

      {/* Mode promotion buttons — commit immediately. */}
      <div>
        <div className="text-xxs uppercase tracking-wide opacity-50 mb-1.5">Mode</div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-1.5">
          {MODE_OPTIONS.map((o) => (
            <button
              key={o.mode}
              className={cn(
                "rounded px-2 py-1.5 text-xs font-medium border transition",
                liveMode === o.mode
                  ? `${o.cls} border-current`
                  : "border-zinc-300 dark:border-zinc-700 hover:bg-zinc-50 dark:hover:bg-zinc-900",
                setMode.isPending && "opacity-50 cursor-wait",
              )}
              disabled={setMode.isPending || liveMode === o.mode}
              onClick={() => { haptic.medium(); setMode.mutate(o.mode); }}
              title={o.help}
            >
              {liveMode === o.mode ? "● " : ""}{o.label}
            </button>
          ))}
        </div>
        <div className="text-xxs opacity-60 mt-1.5 leading-relaxed">
          {MODE_OPTIONS.find((o) => o.mode === liveMode)?.help}
        </div>
        {showAutoWarn && (
          <div className="mt-2 flex items-start gap-1.5 text-xxs p-2 rounded
                          border border-red-300 dark:border-red-900
                          bg-red-50 dark:bg-red-950/30 text-red-700 dark:text-red-300">
            <AlertCircle className="w-3 h-3 mt-0.5 shrink-0" />
            <div>AUTO mode is live. Bracket orders fire automatically when the
              risk manager green-lights an ORB-5 breakout. Halt via the main
              toggle above to stop instantly.</div>
          </div>
        )}
      </div>

      {/* Universe + Scan */}
      <div>
        <div className="text-xxs uppercase tracking-wide opacity-50 mb-1.5">Today's universe</div>
        <div className="flex gap-1.5">
          <input
            type="text"
            value={draft.dt_universe}
            onChange={(e) => setField("dt_universe", e.target.value)}
            placeholder="(empty = no universe filter — engine acts on every subscribed ticker)"
            className="flex-1 px-2 py-1.5 text-xs font-mono rounded border
                       border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-950"
          />
          <button
            onClick={() => { haptic.medium(); scan.mutate(); }}
            disabled={scan.isPending}
            className="btn btn-ghost gap-1"
            title="Run premarket scanner now: gap + premkt volume + catalyst → top tickers"
          >
            {scan.isPending
              ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
              : <Radar className="w-3.5 h-3.5" />}
            Scan
          </button>
        </div>
        <div className="text-xxs opacity-60 mt-1 leading-relaxed">
          Comma-separated tickers the day-trader is allowed to act on. The
          scheduler refreshes this at 08:05 ET via the premarket scanner
          (gap ≥ 4% × log10(premkt vol) × catalyst). Scan button runs it now.
        </div>
        {scan.data && (
          <div className="text-xxs mt-1.5 p-2 rounded border
                          border-zinc-200 dark:border-zinc-800
                          bg-zinc-50 dark:bg-zinc-900/50">
            Scanned <b>{scan.data.scanned}</b>, kept <b>{scan.data.kept}</b>.
            {scan.data.rows.length > 0 && (
              <span className="opacity-70"> Top: {scan.data.rows.slice(0, 3).map((r) =>
                `${r.ticker} (+${r.gap_pct.toFixed(1)}%)`).join(", ")}</span>
            )}
          </div>
        )}
      </div>

      {/* Numeric knobs */}
      <Section title="Strategy & risk">
        <NumRow
          label="DT max concurrent"
          help="Caps how many day-trades the engine may hold open at once. Independent of the swing max_concurrent."
          value={draft.dt_max_concurrent} min={1} max={10} step={1}
          format={(v) => `${v} positions`}
          onChange={(v) => setField("dt_max_concurrent", v)}
        />
        <NumRow
          label="DT risk per trade"
          help="Position size = (budget × pct) / per-share-risk. Default 0.5% — tighter than swing because 1-min stops are smaller."
          value={draft.dt_risk_per_trade_pct} min={0.1} max={5} step={0.1}
          format={(v) => `${v.toFixed(2)}%`}
          onChange={(v) => setField("dt_risk_per_trade_pct", v)}
        />
        <NumRow
          label="Breakout volume × ORB avg"
          help="Bar volume on the breakout bar must be at least this multiple of the opening range's per-bar average."
          value={draft.dt_volume_multiplier} min={1} max={5} step={0.1}
          format={(v) => `${v.toFixed(1)}× ORB avg`}
          onChange={(v) => setField("dt_volume_multiplier", v)}
        />
        <NumRow
          label="Target R-multiple"
          help="Take profit at entry + R × stop distance. Default 2 — i.e. risk $1 to make $2."
          value={draft.dt_target_r_multiple} min={1} max={5} step={0.1}
          format={(v) => `${v.toFixed(1)}R`}
          onChange={(v) => setField("dt_target_r_multiple", v)}
        />
        <NumRow
          label="Time stop"
          help="If neither target nor stop hits within this many bars, exit at market. Default 30 (~30 min)."
          value={draft.dt_time_stop_bars} min={5} max={120} step={5}
          format={(v) => `${v} bars`}
          onChange={(v) => setField("dt_time_stop_bars", v)}
        />
        <ToggleRow
          label="Require breakout close > VWAP"
          help="Only fire if the breakout bar closes above session VWAP (institutional bid alignment)."
          checked={draft.dt_require_vwap}
          onChange={(v) => setField("dt_require_vwap", v)}
        />
        <TimeRow
          label="No new DT entries after"
          help="Last 90 min of session is choppy + leaves no time to manage. Default 14:30 ET."
          value={draft.dt_entry_window_end}
          onChange={(v) => setField("dt_entry_window_end", v)}
        />
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card card-tight space-y-2">
      <div className="text-xxs uppercase tracking-wide opacity-50 mb-1">{title}</div>
      {children}
    </div>
  );
}

function Stat({
  label, value, sub, tone,
}: { label: string; value: string; sub?: string; tone?: "buy" | "sell" }) {
  return (
    <div>
      <div className="opacity-50 uppercase">{label}</div>
      <div className={cn(
        "font-mono font-bold text-sm leading-tight",
        tone === "buy" && "text-buy",
        tone === "sell" && "text-sell",
      )}>{value}</div>
      {sub && <div className="opacity-50 font-mono">{sub}</div>}
    </div>
  );
}

function ToggleRow({
  label, help, checked, onChange, disabled,
}: { label: string; help?: string; checked: boolean;
     onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="flex-1 min-w-0">
        <div className="text-xs font-medium">{label}</div>
        {help && <div className="text-xxs opacity-60 leading-relaxed">{help}</div>}
      </div>
      <button
        role="switch" aria-checked={checked}
        onClick={() => !disabled && onChange(!checked)}
        disabled={disabled}
        className={cn(
          "relative shrink-0 w-10 h-6 rounded-full transition-colors",
          checked ? "bg-buy" : "bg-zinc-300 dark:bg-zinc-700",
          disabled && "opacity-40"
        )}
      >
        <span className={cn(
          "absolute top-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform",
          checked ? "left-[18px]" : "left-0.5"
        )} />
      </button>
    </div>
  );
}

function NumRow({
  label, help, value, step, min, max, onChange, format,
}: { label: string; help?: string; value: number;
     step: number; min?: number; max?: number;
     onChange: (v: number) => void; format: (v: number) => string }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="flex-1 min-w-0">
        <div className="text-xs font-medium">{label}</div>
        {help && <div className="text-xxs opacity-60 leading-relaxed">{help}</div>}
      </div>
      <div className="flex items-center gap-2">
        <input
          type="number"
          value={value}
          step={step} min={min} max={max}
          onChange={(e) => onChange(parseFloat(e.target.value || "0"))}
          className="input !w-28 text-right font-mono"
        />
        <span className="text-xxs opacity-50 w-16">{format(value)}</span>
      </div>
    </div>
  );
}

function TimeRow({
  label, help, value, onChange,
}: { label: string; help?: string; value: string; onChange: (v: string) => void }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="flex-1 min-w-0">
        <div className="text-xs font-medium">{label}</div>
        {help && <div className="text-xxs opacity-60 leading-relaxed">{help}</div>}
      </div>
      <input
        type="time"
        value={value}
        onChange={(e) => onChange(e.target.value || value)}
        className="input !w-28 text-right font-mono"
      />
    </div>
  );
}
