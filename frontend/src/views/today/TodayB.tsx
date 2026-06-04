// Variant B — "Bloomberg Terminal" — dense, no-scroll where possible,
// monospace, color-coded, four panels. Designed for traders who want every
// number visible at once.

import { cn, fmtUSD, fmtPct, shortTime } from "../../lib/cn";
import type { VariantProps } from "./shared";

export function TodayB({ acct, cards, feed, orders, portfolio }: VariantProps) {
  if (!acct) return <div className="card card-tight h-40 animate-pulse" />;
  if (!acct.connected) {
    return <div className="card card-tight border-sell/30 text-sell text-xs">Alpaca disconnected — check .env</div>;
  }

  const pl = acct.todays_pl;
  const plColor = pl >= 0 ? "text-buy" : "text-sell";

  // Split the feed into agent triggers vs news so each panel is focused
  const triggers = (feed ?? []).filter((e) => e.kind === "trigger").slice(0, 12);
  const news = (feed ?? []).filter((e) => e.kind === "news").slice(0, 8);

  return (
    <div className="space-y-3">
      {/* ─── Top status bar (single dense row) ──────────────────────── */}
      <div className="card px-3 py-2 font-mono text-xxs sm:text-xs flex flex-wrap gap-x-4 gap-y-1 items-baseline">
        <span className={cn("font-bold", plColor)}>
          P&L {pl >= 0 ? "+" : ""}{fmtUSD(pl)} ({fmtPct(acct.todays_pl_pct)})
        </span>
        <span><span className="opacity-50">EQ</span> {fmtUSD(acct.equity, 0)}</span>
        <span><span className="opacity-50">CASH</span> {fmtUSD(acct.cash, 0)}</span>
        <span><span className="opacity-50">BP</span> {fmtUSD(acct.buying_power, 0)}</span>
        <span><span className="opacity-50">POS</span> {fmtUSD(acct.positions_value, 0)}</span>
        {acct.thirty_day_return_pct != null && (
          <span><span className="opacity-50">30D</span>{" "}
            <span className={acct.thirty_day_return_pct >= 0 ? "text-buy" : "text-sell"}>
              {fmtPct(acct.thirty_day_return_pct)}
            </span>
          </span>
        )}
        <span className="ml-auto opacity-50">ALPACA · {acct.status}</span>
      </div>

      {/* ─── 2x2 panels ─────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <Panel title={`POSITIONS (${portfolio?.length ?? 0})`}>
          {!portfolio || portfolio.length === 0 ? (
            <Empty msg="No open positions" />
          ) : (
            <TerminalTable
              headers={["TICK", "SHRS", "AVG", "NOW", "P&L%"]}
              rows={portfolio.slice(0, 8).map((p) => [
                <b key="t">{p.ticker}</b>,
                <span key="s" className="opacity-70">{p.shares}</span>,
                fmtUSD(p.avg_price),
                fmtUSD(p.current_price),
                <span key="p" className={p.pnl_pct >= 0 ? "text-buy" : "text-sell"}>
                  {fmtPct(p.pnl_pct)}
                </span>,
              ])}
            />
          )}
        </Panel>

        <Panel title={`WATCHING (${cards?.watchlist.buys ?? 0} BUY · ${cards?.watchlist.holds ?? 0} HOLD)`}>
          {!cards ? (
            <Empty msg="Loading…" />
          ) : cards.watchlist.buys + cards.watchlist.holds === 0 ? (
            <Empty msg="No watchlist — run research cycle" />
          ) : (
            <div className="px-3 py-2 text-xxs font-mono space-y-1.5">
              <div>
                <span className="text-buy font-bold">{cards.watchlist.buys}</span>{" "}
                <span className="opacity-60">BUY signals ready for live agent</span>
              </div>
              <div>
                <span className="opacity-80 font-bold">{cards.watchlist.holds}</span>{" "}
                <span className="opacity-60">HOLDs near threshold</span>
              </div>
              {cards.watchlist.sells > 0 && (
                <div>
                  <span className="text-sell font-bold">{cards.watchlist.sells}</span>{" "}
                  <span className="opacity-60">SELL signals</span>
                </div>
              )}
              <hr className="border-zinc-200 dark:border-zinc-800 my-2" />
              <div className="opacity-60">
                AGENT {cards.agent.running
                  ? <span className="text-buy">● RUNNING</span>
                  : <span>○ STOPPED</span>}
                {" · "}{cards.agent.subscribed_tickers} tickers
                {" · "}BUYS {cards.agent.buys_fired_today}/{cards.agent.buys_cap}
              </div>
            </div>
          )}
        </Panel>

        <Panel title={`AGENT DECISIONS (24h · ${triggers.length})`}>
          {triggers.length === 0 ? (
            <Empty msg="No triggers — market closed or agent idle" />
          ) : (
            <div className="px-3 py-1.5 text-xxs font-mono space-y-0.5">
              {triggers.map((e, i) => (
                <div key={i} className="flex items-baseline gap-2">
                  <span className="opacity-50 w-10">{shortTime(e.ts)}</span>
                  <span className="font-bold w-12">{e.ticker}</span>
                  <span className={cn("w-12",
                    e.direction === "BUY" ? "text-buy" : "text-sell")}>{e.direction}</span>
                  <span className="opacity-70 truncate flex-1">
                    {e.action?.replace(/_/g, " ")}
                    {e.score_before != null && e.score_after != null && ` ${e.score_before}→${e.score_after}`}
                    {e.price && ` @ ${fmtUSD(e.price)}`}
                  </span>
                </div>
              ))}
            </div>
          )}
        </Panel>

        <Panel title={`NEWS SHOCKS (24h · ${news.length})`}>
          {news.length === 0 ? (
            <Empty msg="No non-neutral news in last 24h" />
          ) : (
            <div className="px-3 py-1.5 text-xxs font-mono space-y-1">
              {news.map((e, i) => (
                <div key={i} className="flex items-baseline gap-2">
                  <span className={cn(
                    "shrink-0 w-12 font-bold",
                    e.classification === "POSITIVE_BOOST"   && "text-buy",
                    e.classification === "NEGATIVE_WARNING" && "text-amber-500",
                    e.classification === "NEGATIVE_BLOCK"   && "text-sell",
                  )}>{e.ticker}</span>
                  <span className="opacity-80 line-clamp-2 leading-tight">{e.headline}</span>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>

      {/* ─── Pending orders strip (only if any) ────────────────────── */}
      {orders && orders.length > 0 && (
        <Panel title={`PENDING ORDERS (${orders.length})`}>
          <TerminalTable
            headers={["TICK", "SIDE", "QTY", "TYPE", "STATUS"]}
            rows={orders.map((o) => [
              <b key="t">{o.symbol}</b>,
              <span key="s" className={o.side === "BUY" ? "text-buy" : "text-sell"}>{o.side}</span>,
              o.qty,
              o.is_bracket ? "BRACKET" : o.order_type,
              <span key="st" className="opacity-60">{o.status}</span>,
            ])}
          />
        </Panel>
      )}
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card overflow-hidden">
      <div className="px-3 py-1.5 border-b border-zinc-200 dark:border-zinc-800
                      bg-zinc-50 dark:bg-zinc-900/50 text-xxs font-mono font-bold opacity-80">
        {title}
      </div>
      {children}
    </div>
  );
}

function Empty({ msg }: { msg: string }) {
  return <div className="px-3 py-4 text-xxs opacity-50 text-center font-mono">{msg}</div>;
}

function TerminalTable({ headers, rows }: { headers: string[]; rows: Array<Array<React.ReactNode>> }) {
  return (
    <table className="w-full text-xxs font-mono">
      <thead>
        <tr className="opacity-50">
          {headers.map((h, i) => (
            <th key={i} className={cn("px-3 py-1 text-left", i > 1 && "text-right")}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((cells, ri) => (
          <tr key={ri} className="border-t border-zinc-100 dark:border-zinc-800/50">
            {cells.map((c, ci) => (
              <td key={ci} className={cn("px-3 py-1", ci > 1 && "text-right")}>{c}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
