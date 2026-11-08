// Mirrors backend/schemas.py. The QueryResult block is the FROZEN contract from the plan:
// change it only together with schemas.py and after telling the team.

export type Tier = "high" | "medium" | "low";
export type JudgeChoice = "relevant" | "not_relevant" | "unsure";

export interface Criteria {
  asset_type: string;
  event: { name: string; date: string; terms: string[] };
  window: { baseline_days: number; recent_days: number };
  status: string[];
  region: string;
}

export interface Channel {
  id: string;
  reason: string;
}

export interface Components {
  exposure: number;
  news_z: number;
  tone_shift: number;
  attention_z: number;
  relevant_articles: number;
}

export interface Evidence {
  title: string;
  url: string;
  date: string;
  judge: JudgeChoice;
  confidence: number;
}

export interface Result {
  id: string;
  name: string;
  lat: number;
  lon: number;
  tier: Tier;
  score: number;
  components: Components;
  paths: string[][];
  reason: string;
  evidence: Evidence[];
}

export interface QueryResult {
  criteria: Criteria;
  channels: Channel[];
  results: Result[];
  dark_count: number;
}

// ---- proposed shapes (not in the plan's contract)
export interface Preset {
  id: string;
  label: string;
  query: string;
  kind: "hero" | "weather" | "labour" | "negative_control" | "other";
}

export interface AssetPoint {
  id: string;
  name: string;
  lat: number;
  lon: number;
  country: string | null;
}

export interface PathNode {
  id: string;
  type: string;
  name: string;
  lat: number | null;
  lon: number | null;
}

export interface AssetDetail {
  id: string;
  name: string;
  path_nodes: PathNode[];
  paths: string[][];
  series: { dates: string[]; news_volume: number[]; tone: number[]; pageviews: number[]; recent_days: number } | null;
  evidence: Evidence[];
}

export interface EvalReport {
  judge: { method: string; precision: number | null; recall: number | null; n: number }[];
  agreement_kappa: number | null;
  backtests: { event: string; method: string; precision_at_10: number | null }[];
  negative_controls: { query: string; lit: number; dark: number; passed: boolean }[];
  note: string;
}
