import { ZodError, z } from "zod/v4";
import { JobListingSchema, JobStatusSchema } from "../types";
import { type JobLedger, PROCESSED_JOB_OUTCOMES } from "./jobLedger";

export const JOB_LEDGER_RPC_URL = "http://job-ledger.internal/rpc";

const ProcessedJobSchema = z.object({
  sourceKey: z.string(),
  rawUrl: z.string().nullable(),
  company: z.string(),
  title: z.string(),
  outcome: z.enum(PROCESSED_JOB_OUTCOMES),
  firstProcessedAt: z.string(),
  lastProcessedAt: z.string(),
  traceId: z.string().nullable(),
});

const CompanyExclusionSchema = z.object({
  company: z.string(),
  excludedAt: z.string(),
});

const PendingNotionProjectionSchema = z.object({
  sourceKey: z.string(),
  job: JobListingSchema,
  status: JobStatusSchema,
  createdAt: z.string(),
});

const PendingNotionProjectionInputSchema = z.object({
  job: JobListingSchema,
  status: JobStatusSchema,
  createdAt: z.string(),
});

const RecordProcessedJobInputSchema = z.object({
  rawUrl: z.string().optional(),
  sourceKey: z.string().optional(),
  company: z.string(),
  title: z.string(),
  outcome: z.enum(PROCESSED_JOB_OUTCOMES),
  processedAt: z.string().optional(),
  traceId: z.string().optional(),
  pendingNotionProjection: PendingNotionProjectionInputSchema.optional(),
});

const NotionBackfillStatsSchema = z.object({
  sourceRows: z.number(),
  urls: z.number(),
  companyTitlePairs: z.number(),
  urlLessRows: z.number(),
  exclusions: z.number(),
});

const JobLedgerRpcRequestSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("findByRawUrl"), rawUrl: z.string() }),
  z.object({ kind: z.literal("titlesForCompany"), company: z.string() }),
  z.object({ kind: z.literal("findCompanyExclusion"), company: z.string() }),
  z.object({ kind: z.literal("recordProcessedJob"), input: RecordProcessedJobInputSchema }),
  z.object({ kind: z.literal("listPendingNotionProjections") }),
  z.object({ kind: z.literal("markNotionProjectionComplete"), sourceKey: z.string() }),
  z.object({
    kind: z.literal("excludeCompany"),
    input: z.object({
      company: z.string(),
      excludedAt: z.string().optional(),
      sourceKey: z.string().optional(),
    }),
  }),
  z.object({ kind: z.literal("notionBackfillStats") }),
  z.object({ kind: z.literal("markMigration"), name: z.string(), completedAt: z.string() }),
  z.object({ kind: z.literal("hasMigration"), name: z.string() }),
]);

type JobLedgerRpcRequest = z.infer<typeof JobLedgerRpcRequestSchema>;
interface RpcResponse {
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
}

type RpcFetch = (
  url: string,
  init: { method: "POST"; headers: Record<string, string>; body: string },
) => Promise<RpcResponse>;

const resultSchemas = {
  findByRawUrl: ProcessedJobSchema.nullable(),
  titlesForCompany: z.array(z.string()),
  findCompanyExclusion: CompanyExclusionSchema.nullable(),
  recordProcessedJob: PendingNotionProjectionSchema.nullable(),
  listPendingNotionProjections: z.array(PendingNotionProjectionSchema),
  markNotionProjectionComplete: z.null(),
  excludeCompany: z.null(),
  notionBackfillStats: NotionBackfillStatsSchema,
  markMigration: z.null(),
  hasMigration: z.boolean(),
} satisfies Record<JobLedgerRpcRequest["kind"], z.ZodType>;

export function createHttpJobLedger(
  rpcFetch: RpcFetch = (url, init) => fetch(url, init),
): JobLedger {
  return {
    findByRawUrl: (rawUrl) =>
      callRpc(rpcFetch, { kind: "findByRawUrl", rawUrl }, resultSchemas.findByRawUrl),
    titlesForCompany: (company) =>
      callRpc(rpcFetch, { kind: "titlesForCompany", company }, resultSchemas.titlesForCompany),
    findCompanyExclusion: (company) =>
      callRpc(
        rpcFetch,
        { kind: "findCompanyExclusion", company },
        resultSchemas.findCompanyExclusion,
      ),
    recordProcessedJob: (input) =>
      callRpc(rpcFetch, { kind: "recordProcessedJob", input }, resultSchemas.recordProcessedJob),
    listPendingNotionProjections: () =>
      callRpc(
        rpcFetch,
        { kind: "listPendingNotionProjections" },
        resultSchemas.listPendingNotionProjections,
      ),
    markNotionProjectionComplete: async (sourceKey) => {
      await callRpc(
        rpcFetch,
        { kind: "markNotionProjectionComplete", sourceKey },
        resultSchemas.markNotionProjectionComplete,
      );
    },
    excludeCompany: async (input) => {
      await callRpc(rpcFetch, { kind: "excludeCompany", input }, resultSchemas.excludeCompany);
    },
    notionBackfillStats: () =>
      callRpc(rpcFetch, { kind: "notionBackfillStats" }, resultSchemas.notionBackfillStats),
    markMigration: async (name, completedAt) => {
      await callRpc(
        rpcFetch,
        { kind: "markMigration", name, completedAt },
        resultSchemas.markMigration,
      );
    },
    hasMigration: (name) =>
      callRpc(rpcFetch, { kind: "hasMigration", name }, resultSchemas.hasMigration),
  };
}

export async function handleJobLedgerRpc(request: Request, ledger: JobLedger): Promise<Response> {
  if (request.method !== "POST" || new URL(request.url).pathname !== "/rpc") {
    return Response.json({ error: "not found" }, { status: 404 });
  }

  let operation: JobLedgerRpcRequest;
  try {
    const body: unknown = await request.json();
    operation = JobLedgerRpcRequestSchema.parse(body);
  } catch (error) {
    if (error instanceof ZodError || error instanceof SyntaxError) {
      return Response.json({ error: "invalid request" }, { status: 400 });
    }
    throw error;
  }

  try {
    const result = await executeOperation(ledger, operation);
    return Response.json({ kind: operation.kind, result });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Job ledger operation failed";
    return Response.json({ error: message }, { status: 500 });
  }
}

async function callRpc<K extends JobLedgerRpcRequest["kind"], S extends z.ZodType>(
  rpcFetch: RpcFetch,
  operation: Extract<JobLedgerRpcRequest, { kind: K }>,
  resultSchema: S,
): Promise<z.output<S>> {
  const response = await rpcFetch(JOB_LEDGER_RPC_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(operation),
  });
  const body: unknown = await response.json();
  if (!response.ok) {
    const error = z.object({ error: z.string() }).safeParse(body);
    throw new Error(
      error.success ? error.data.error : `Job ledger RPC failed with status ${response.status}`,
    );
  }
  const envelope = z.object({ kind: z.literal(operation.kind), result: z.unknown() }).parse(body);
  return resultSchema.parse(envelope.result);
}

async function executeOperation(
  ledger: JobLedger,
  operation: JobLedgerRpcRequest,
): Promise<unknown> {
  switch (operation.kind) {
    case "findByRawUrl":
      return resultSchemas.findByRawUrl.parse(await ledger.findByRawUrl(operation.rawUrl));
    case "titlesForCompany":
      return resultSchemas.titlesForCompany.parse(await ledger.titlesForCompany(operation.company));
    case "findCompanyExclusion":
      return resultSchemas.findCompanyExclusion.parse(
        await ledger.findCompanyExclusion(operation.company),
      );
    case "recordProcessedJob":
      return resultSchemas.recordProcessedJob.parse(
        await ledger.recordProcessedJob(operation.input),
      );
    case "listPendingNotionProjections":
      return resultSchemas.listPendingNotionProjections.parse(
        await ledger.listPendingNotionProjections(),
      );
    case "markNotionProjectionComplete":
      await ledger.markNotionProjectionComplete(operation.sourceKey);
      return null;
    case "excludeCompany":
      await ledger.excludeCompany(operation.input);
      return null;
    case "notionBackfillStats":
      return resultSchemas.notionBackfillStats.parse(await ledger.notionBackfillStats());
    case "markMigration":
      await ledger.markMigration(operation.name, operation.completedAt);
      return null;
    case "hasMigration":
      return resultSchemas.hasMigration.parse(await ledger.hasMigration(operation.name));
  }
}
