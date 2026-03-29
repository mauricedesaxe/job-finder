export type Stage = "evaluation" | "enrichment" | "dedup";

export interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
}

export interface StageUsage {
  input: number;
  output: number;
  calls: number;
}

export interface TokenSummary {
  byStage: Record<Stage, StageUsage>;
  total: { input: number; output: number; calls: number };
  estimatedCost: number;
}

// Haiku pricing (USD per million tokens)
const INPUT_COST_PER_MTOK = 0.8;
const OUTPUT_COST_PER_MTOK = 4.0;

const STAGES: Stage[] = ["evaluation", "enrichment", "dedup"];

function emptyStage(): StageUsage {
  return { input: 0, output: 0, calls: 0 };
}

export class TokenTracker {
  private stages: Record<Stage, StageUsage>;

  constructor() {
    this.stages = {
      evaluation: emptyStage(),
      enrichment: emptyStage(),
      dedup: emptyStage(),
    };
  }

  add(stage: Stage, usage: TokenUsage): void {
    const s = this.stages[stage];
    s.input += usage.input_tokens;
    s.output += usage.output_tokens;
    s.calls++;
  }

  summary(): TokenSummary {
    const total = { input: 0, output: 0, calls: 0 };
    for (const stage of STAGES) {
      total.input += this.stages[stage].input;
      total.output += this.stages[stage].output;
      total.calls += this.stages[stage].calls;
    }

    const estimatedCost =
      (total.input / 1_000_000) * INPUT_COST_PER_MTOK +
      (total.output / 1_000_000) * OUTPUT_COST_PER_MTOK;

    return {
      byStage: { ...this.stages },
      total,
      estimatedCost,
    };
  }
}
