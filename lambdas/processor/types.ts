// types.ts

export interface NewsArticlePayload {
  /** News provider identifier (e.g. finlight, finnhub, etc.) */
  provider: "finlight";

  /** Headline of the news article */
  headline: string;

  /** Source or publisher name (e.g. Reuters, Bloomberg) */
  source?: string;

  /** Canonical URL to the article */
  url?: string;

  /** ISO timestamp of publication */
  publishedAt: string;

  /** Short summary or excerpt if provided by the feed */
  summary?: string;

  /** Optional structured list of companies or entities mentioned */
  companies?: Record<string, any>[];

  /** Raw provider payload for debugging or future enrichment */
  raw?: Record<string, any>;
}
