import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Settings, Save, Loader2, AlertCircle, Power, OctagonX, Eraser, CheckCircle2,
} from "lucide-react";
import { api, type AgentConfig, type AutoTradeStatus } from "../lib/api";
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
