import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { AIMessage } from "@langchain/core/messages";
import { RunnableLambda } from "@langchain/core/runnables";
import { Client } from "langsmith";
import {
  getPromptCommitHash,
  getPromptReleaseTag,
  initLangSmith,
  LangSmithTraceUnavailableError,
  shutdownLangSmith,
  traced,
} from "../langsmith";
import { PROMPT_NAMES } from "../promptRegistry";

describe("LangSmith prompt resolution", () => {
  afterEach(shutdownLangSmith);

  test("resolves release tags from the repository tag array", async () => {
    const fetch = spyOn(globalThis, "fetch");
    for (const _ of PROMPT_NAMES) {
      fetch.mockResolvedValueOnce(
        new Response(
          JSON.stringify([{ tag_name: "release-2026-08-23-1", commit_hash: "release-commit" }]),
        ),
      );
    }

    try {
      await initLangSmith(
        {
          apiKey: "key",
          endpoint: "https://example.test",
          project: "test",
          openrouterApiKey: "router",
        },
        {
          pullPrompt: async () => RunnableLambda.from(async () => new AIMessage({ content: "" })),
        },
      );

      expect(getPromptReleaseTag()).toBe("release-2026-08-23-1");
      expect(getPromptCommitHash(PROMPT_NAMES[0])).toBe("release-commit");
    } finally {
      fetch.mockRestore();
    }
  });

  test("freezes every production prompt at one release", async () => {
    await initLangSmith(
      {
        apiKey: "key",
        endpoint: "https://example.test",
        project: "test",
        openrouterApiKey: "router",
      },
      {
        resolvePrompt: async ({ name }) => ({
          releaseTags: [{ name: "release-2026-08-23-1", commitHash: `${name}-commit` }],
        }),
        pullPrompt: async () => RunnableLambda.from(async () => new AIMessage({ content: "" })),
      },
    );
    expect(getPromptReleaseTag()).toBe("release-2026-08-23-1");
    expect(getPromptCommitHash(PROMPT_NAMES[0])).toBe(`${PROMPT_NAMES[0]}-commit`);
  });
  test("rejects a mixed production release before work starts", async () => {
    await expect(
      initLangSmith(
        {
          apiKey: "key",
          endpoint: "https://example.test",
          project: "test",
          openrouterApiKey: "router",
        },
        {
          resolvePrompt: async ({ name }) => ({
            releaseTags: [
              {
                name: name === PROMPT_NAMES[0] ? "release-2026-08-23-1" : "release-2026-08-22-1",
                commitHash: `${name}-commit`,
              },
            ],
          }),
          pullPrompt: async () => RunnableLambda.from(async () => new AIMessage({ content: "" })),
        },
      ),
    ).rejects.toThrow("mixed releases");
  });

  test("cleans a newly created client when initialization fails", async () => {
    const cleanup = spyOn(Client.prototype, "cleanup").mockImplementation(() => {});
    try {
      await expect(
        initLangSmith(
          {
            apiKey: "key",
            endpoint: "https://example.test",
            project: "test",
            openrouterApiKey: "router",
          },
          {
            resolvePrompt: async () => {
              throw new Error("prompt resolution failed");
            },
          },
        ),
      ).rejects.toThrow("prompt resolution failed");
      expect(cleanup).toHaveBeenCalledTimes(1);
    } finally {
      cleanup.mockRestore();
    }
  });

  test("selects the common release when unchanged prompts retain older tags", async () => {
    await initLangSmith(
      {
        apiKey: "key",
        endpoint: "https://example.test",
        project: "test",
        openrouterApiKey: "router",
      },
      {
        resolvePrompt: async ({ name }) => ({
          releaseTags:
            name === PROMPT_NAMES[0]
              ? [
                  { name: "release-2026-08-22-1", commitHash: `${name}-old-commit` },
                  { name: "release-2026-08-23-1", commitHash: `${name}-commit` },
                ]
              : [{ name: "release-2026-08-23-1", commitHash: `${name}-commit` }],
        }),
        pullPrompt: async () => RunnableLambda.from(async () => new AIMessage({ content: "" })),
      },
    );

    expect(getPromptReleaseTag()).toBe("release-2026-08-23-1");
  });
});

describe("LangSmith trace acceptance", () => {
  afterEach(shutdownLangSmith);

  test("rejects an unaccepted trace when batch export fails", async () => {
    const consoleError = spyOn(console, "error").mockImplementation(() => {});
    const client = traceClient((url) =>
      url.endsWith("/info")
        ? Response.json({ batch_ingest_config: { use_multipart_endpoint: false } })
        : url.endsWith("/runs/batch")
          ? new Response("unavailable", { status: 503 })
          : new Response("missing", { status: 404 }),
    );
    await initTestLangSmith(client);

    try {
      let recorded = false;
      await expect(
        traced({ name: "process_job" }, async ({ requireAccepted }) => {
          await requireAccepted();
          recorded = true;
          return { data: "inserted" };
        }),
      ).rejects.toBeInstanceOf(LangSmithTraceUnavailableError);

      expect(recorded).toBe(false);
      expect(consoleError).toHaveBeenCalled();
    } finally {
      consoleError.mockRestore();
    }
  }, 20_000);

  test("keeps concurrent child runs under one root trace", async () => {
    const startedRuns: Array<Record<string, unknown>> = [];
    const client = traceClient((url, init) => {
      if (url.endsWith("/info")) {
        return Response.json({ batch_ingest_config: { use_multipart_endpoint: false } });
      }
      if (url.endsWith("/runs/batch")) {
        const body = init?.body;
        const text =
          typeof body === "string"
            ? body
            : body instanceof Uint8Array
              ? new TextDecoder().decode(body)
              : "";
        const batch = JSON.parse(text) as { post?: Array<Record<string, unknown>> };
        startedRuns.push(...(batch.post ?? []));
        return new Response(null, { status: 202 });
      }
      return Response.json({
        id: "11111111-1111-4111-8111-111111111111",
        trace_id: "11111111-1111-4111-8111-111111111111",
        name: "evaluation_fixture",
        run_type: "chain",
        start_time: "2026-08-24T00:00:00Z",
        inputs: {},
        extra: {},
      });
    });
    await initTestLangSmith(client);

    await traced({ name: "evaluation_fixture" }, async ({ requireAccepted }) => {
      await requireAccepted();
      await Promise.all([
        traced({ name: "filter" }, async () => ({ data: "filter" })),
        traced({ name: "profile" }, async () => ({ data: "profile" })),
      ]);
      return { data: "complete" };
    });
    await client.flush();

    const uniqueRuns = [...new Map(startedRuns.map((run) => [run.id, run])).values()];
    const roots = uniqueRuns.filter((run) => run.parent_run_id == null);
    expect(roots).toHaveLength(1);
    const root = roots[0];
    expect(root?.name).toBe("evaluation_fixture");
    expect(new Set(uniqueRuns.map((run) => run.trace_id))).toEqual(new Set([root?.id]));
    expect(uniqueRuns.filter((run) => run.id !== root?.id).every((run) => run.parent_run_id)).toBe(
      true,
    );
  });

  test("persists work inside the parent after trace acceptance", async () => {
    const events: string[] = [];
    const client = traceClient((url) => {
      if (url.endsWith("/info")) {
        return Response.json({ batch_ingest_config: { use_multipart_endpoint: false } });
      }
      if (url.endsWith("/runs/batch")) {
        events.push("batch");
        return new Response(null, { status: 202 });
      }
      events.push("read");
      return Response.json({
        id: "11111111-1111-4111-8111-111111111111",
        trace_id: "11111111-1111-4111-8111-111111111111",
        name: "process_job",
        run_type: "chain",
        start_time: "2026-08-24T00:00:00Z",
        inputs: {},
        extra: {},
      });
    });
    await initTestLangSmith(client);

    await traced({ name: "process_job" }, async ({ requireAccepted }) => {
      await requireAccepted();
      events.push("persist");
      return { data: "inserted" };
    });

    expect(events.slice(0, 3)).toEqual(["batch", "read", "persist"]);
  });

  test("retries missing traces before persisting accepted work", async () => {
    const events: string[] = [];
    let reads = 0;
    const client = traceClient((url) => {
      if (url.endsWith("/info")) {
        return Response.json({ batch_ingest_config: { use_multipart_endpoint: false } });
      }
      if (url.endsWith("/runs/batch")) {
        events.push("batch");
        return new Response(null, { status: 202 });
      }
      reads++;
      events.push(`read-${reads}`);
      if (reads <= 3) return new Response("missing", { status: 404 });
      return Response.json({
        id: "11111111-1111-4111-8111-111111111111",
        trace_id: "11111111-1111-4111-8111-111111111111",
        name: "process_job",
        run_type: "chain",
        start_time: "2026-08-24T00:00:00Z",
        inputs: {},
        extra: {},
      });
    });
    await initTestLangSmith(client);

    await traced({ name: "process_job" }, async ({ requireAccepted }) => {
      await requireAccepted();
      events.push("persist");
      return { data: "inserted" };
    });

    expect(events.slice(0, 6)).toEqual([
      "batch",
      "read-1",
      "read-2",
      "read-3",
      "read-4",
      "persist",
    ]);
  });

  test("paces physical trace GET starts for supported endpoints", async () => {
    expect(
      await physicalTraceReadStarts({ endpoint: "https://example.test", failFirst: false }),
    ).toEqual([0, 2_100, 4_200]);
    expect(
      await physicalTraceReadStarts({
        endpoint: "https://example.test/api/v1",
        failFirst: false,
      }),
    ).toEqual([0, 2_100, 4_200]);
  });

  test("paces SDK retries after a failed physical trace GET", async () => {
    expect(
      await physicalTraceReadStarts({ endpoint: "https://example.test", failFirst: true }),
    ).toEqual([0, 2_100, 4_200, 6_300]);
  });

  test("fails immediately when a trace read returns a non-404 error", async () => {
    const events: string[] = [];
    const client = traceClient((url) => {
      if (url.endsWith("/info")) {
        return Response.json({ batch_ingest_config: { use_multipart_endpoint: false } });
      }
      if (url.endsWith("/runs/batch")) {
        events.push("batch");
        return new Response(null, { status: 202 });
      }
      events.push("read");
      return new Response("unauthorized", { status: 401 });
    });
    await initTestLangSmith(client);

    let persisted = false;
    await expect(
      traced({ name: "process_job" }, async ({ requireAccepted }) => {
        await requireAccepted();
        persisted = true;
        return { data: "inserted" };
      }),
    ).rejects.toBeInstanceOf(LangSmithTraceUnavailableError);

    expect(events).toEqual(["batch", "read"]);
    expect(persisted).toBe(false);
  });

  test("runs deferred work only after the parent trace completes", async () => {
    const events: string[] = [];
    let reads = 0;
    const client = traceClient((url) => {
      if (url.endsWith("/info")) {
        return Response.json({ batch_ingest_config: { use_multipart_endpoint: false } });
      }
      if (url.endsWith("/runs/batch")) {
        events.push("batch");
        return new Response(null, { status: 202 });
      }
      reads++;
      events.push(reads === 1 ? "read-open" : "read-complete");
      return Response.json({
        id: "11111111-1111-4111-8111-111111111111",
        trace_id: "11111111-1111-4111-8111-111111111111",
        session_id: "22222222-2222-4222-8222-222222222222",
        name: "process_job",
        run_type: "chain",
        start_time: "2026-08-24T00:00:00Z",
        end_time: reads === 1 ? null : "2026-08-24T00:00:01Z",
        inputs: {},
        extra: {},
      });
    });
    await initTestLangSmith(client);

    await traced({ name: "process_job" }, async ({ requireAccepted }) => {
      await requireAccepted();
      events.push("callback");
      return {
        data: "inserted",
        afterTraceComplete: async () => {
          events.push("after-complete");
        },
      };
    });

    expect(events).toEqual([
      "batch",
      "read-open",
      "callback",
      "batch",
      "read-complete",
      "after-complete",
    ]);
  });

  test("extends retention for operational errors", async () => {
    let reads = 0;
    let feedback: Record<string, unknown> | undefined;
    const client = traceClient((url, init) => {
      if (url.endsWith("/info")) {
        return Response.json({ batch_ingest_config: { use_multipart_endpoint: false } });
      }
      if (url.endsWith("/runs/batch")) return new Response(null, { status: 202 });
      if (url.endsWith("/feedback")) {
        feedback = JSON.parse(String(init?.body));
        return new Response(null, { status: 202 });
      }
      reads++;
      return Response.json({
        id: "11111111-1111-4111-8111-111111111111",
        trace_id: "11111111-1111-4111-8111-111111111111",
        session_id: "22222222-2222-4222-8222-222222222222",
        name: "process_job",
        run_type: "chain",
        start_time: "2026-08-24T00:00:00Z",
        end_time: reads === 1 ? null : "2026-08-24T00:00:01Z",
        inputs: {},
        extra: {},
      });
    });
    await initTestLangSmith(client);

    await expect(
      traced({ name: "process_job" }, async ({ requireAccepted }) => {
        await requireAccepted();
        throw new Error("Notion unavailable");
      }),
    ).rejects.toThrow("Notion unavailable");

    expect(feedback).toMatchObject({
      key: "operational_error",
      value: "Notion unavailable",
      extend_trace_retention: true,
    });
  });
});

function traceClient(fetchImplementation: (url: string, init?: RequestInit) => Response): Client {
  const testFetch = Object.assign(
    async (url: string | URL | Request, init?: RequestInit) =>
      fetchImplementation(String(url), init),
    { preconnect: (_url: string | URL) => {} },
  );
  return new Client({
    apiUrl: "https://example.test",
    apiKey: "key",
    callerOptions: { maxRetries: 0 },
    fetchImplementation: testFetch,
  });
}

async function initTestLangSmith(client: Client): Promise<void> {
  await initLangSmith(
    {
      apiKey: "key",
      endpoint: "https://example.test",
      project: "test",
      openrouterApiKey: "router",
    },
    {
      client,
      resolvePrompt: async ({ name }) => ({
        releaseTags: [{ name: "release-2026-08-23-1", commitHash: `${name}-commit` }],
      }),
      pullPrompt: async () => RunnableLambda.from(async () => new AIMessage({ content: "" })),
    },
  );
}

async function physicalTraceReadStarts(input: {
  endpoint: string;
  failFirst: boolean;
}): Promise<number[]> {
  let now = 0;
  let traceReadAttempts = 0;
  const traceReadStarts: number[] = [];
  const testFetch = Object.assign(
    async (request: string | URL | Request) => {
      const url = new URL(String(request));
      if (/^\/(?:api\/v1\/)?runs\/[0-9a-f-]{36}$/.test(url.pathname)) {
        traceReadStarts.push(now);
        traceReadAttempts++;
        if (input.failFirst && traceReadAttempts === 1) {
          return new Response("rate limited", { status: 429 });
        }
        const traceId = url.pathname.split("/").at(-1);
        return Response.json({
          id: traceId,
          trace_id: traceId,
          name: "process_job",
          run_type: "chain",
          start_time: "2026-08-24T00:00:00Z",
          inputs: {},
          extra: {},
        });
      }
      if (url.pathname.endsWith("/info")) {
        return Response.json({ batch_ingest_config: { use_multipart_endpoint: false } });
      }
      return new Response(null, { status: 202 });
    },
    { preconnect: (_url: string | URL) => {} },
  );
  try {
    await initLangSmith(
      {
        apiKey: "key",
        endpoint: input.endpoint,
        project: "test",
        openrouterApiKey: "router",
      },
      {
        fetchImplementation: testFetch,
        traceReadScheduler: {
          now: () => now,
          sleep: async (delayMs: number) => {
            now += delayMs;
          },
        },
        resolvePrompt: async ({ name }) => ({
          releaseTags: [{ name: "release-2026-08-23-1", commitHash: `${name}-commit` }],
        }),
        pullPrompt: async () => RunnableLambda.from(async () => new AIMessage({ content: "" })),
      },
    );
    await Promise.all(
      Array.from({ length: 3 }, (_, index) =>
        traced({ name: "process_job" }, async ({ requireAccepted }) => {
          await requireAccepted();
          return { data: index };
        }),
      ),
    );
    return traceReadStarts;
  } finally {
    shutdownLangSmith();
  }
}
