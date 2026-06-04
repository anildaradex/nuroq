// Shared props for all 4 Today variants. The shell fetches once and passes
// the same data into each — so flipping variants is instant + identical.

import type {
  AlpacaSummary, EquityHistory, TodayCards, NextAction,
  FeedEvent, PendingOrder, PortfolioRow,
} from "../../lib/api";

export interface VariantProps {
  acct: AlpacaSummary | undefined;
  history: EquityHistory | undefined;
  cards: TodayCards | undefined;
  nextActions: NextAction[] | undefined;
  feed: FeedEvent[] | undefined;
  orders: PendingOrder[] | undefined;
  portfolio: PortfolioRow[] | undefined;
}
