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

export interface ModelUsage {
  byStage: Record<Stage, StageUsage>;
  total: StageUsage;
}

export interface TokenSummary {
  byModel: Record<string, ModelUsage>;
  total: StageUsage;
}

const STAGES: Stage[] = ["evaluation", "enrichment", "dedup"];

function emptyStage(): StageUsage {
  return { input: 0, output: 0, calls: 0 };
}

function emptyModelUsage(): Record<Stage, StageUsage> {
  return {
    evaluation: emptyStage(),
    enrichment: emptyStage(),
    dedup: emptyStage(),
  };
}

export class TokenTracker {
  private models = new Map<string, Record<Stage, StageUsage>>();

  add(model: string, stage: Stage, usage: TokenUsage): void {
    let stages = this.models.get(model);
    if (!stages) {
      stages = emptyModelUsage();
      this.models.set(model, stages);
    }
    const s = stages[stage];
    s.input += usage.input_tokens;
    s.output += usage.output_tokens;
    s.calls++;
  }

  summary(): TokenSummary {
    const byModel: Record<string, ModelUsage> = {};
    const total: StageUsage = { input: 0, output: 0, calls: 0 };

    for (const [model, stages] of this.models) {
      const modelTotal: StageUsage = { input: 0, output: 0, calls: 0 };
      for (const stage of STAGES) {
        modelTotal.input += stages[stage].input;
        modelTotal.output += stages[stage].output;
        modelTotal.calls += stages[stage].calls;
      }
      byModel[model] = { byStage: { ...stages }, total: modelTotal };
      total.input += modelTotal.input;
      total.output += modelTotal.output;
      total.calls += modelTotal.calls;
    }

    return { byModel, total };
  }
}
