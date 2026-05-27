import { Client } from "@notionhq/client";
import type {
  CreatePageParameters,
  CreatePageResponse,
  GetDatabaseParameters,
  GetDatabaseResponse,
  QueryDatabaseParameters,
  QueryDatabaseResponse,
  UpdateDatabaseParameters,
  UpdateDatabaseResponse,
  UpdatePageParameters,
  UpdatePageResponse,
} from "@notionhq/client/build/src/api-endpoints";
import { isRetryableNotion, notionBreaker, notionRateLimiter, withRetry } from "../../concurrency";
import { logger } from "../../logger";

const log = logger.child({ component: "notion" });

/**
 * The slice of the Notion SDK the app actually uses. Mirroring the SDK shape
 * means call sites keep calling `client.databases.query({...})` unchanged — the
 * only difference is that every call is now wrapped in the resilience stack.
 */
export interface ResilientNotionClient {
  databases: {
    query: (args: QueryDatabaseParameters) => Promise<QueryDatabaseResponse>;
    retrieve: (args: GetDatabaseParameters) => Promise<GetDatabaseResponse>;
    update: (args: UpdateDatabaseParameters) => Promise<UpdateDatabaseResponse>;
  };
  pages: {
    create: (args: CreatePageParameters) => Promise<CreatePageResponse>;
    update: (args: UpdatePageParameters) => Promise<UpdatePageResponse>;
  };
}

/**
 * Rate limit → circuit breaker → retry, the same stack the rest of the app uses
 * for upstreams. Exported for unit testing the retry behaviour directly.
 */
export function withNotionResilience<T>(fn: () => Promise<T>): Promise<T> {
  return notionRateLimiter.run(() =>
    notionBreaker.run(() =>
      withRetry(fn, {
        shouldRetry: isRetryableNotion,
        onRetry: (attempt, err) => log.warn({ attempt, err }, "notion retry"),
      }),
    ),
  );
}

/**
 * The ONLY Notion client the app constructs. Returns a resilient wrapper rather
 * than the raw SDK client, so no call site — pipeline stage, reconcile, cache
 * build, or script — can issue an unthrottled, un-retried Notion request.
 */
export function createNotionClient(token: string): ResilientNotionClient {
  const client = new Client({ auth: token });
  return {
    databases: {
      query: (args) => withNotionResilience(() => client.databases.query(args)),
      retrieve: (args) => withNotionResilience(() => client.databases.retrieve(args)),
      update: (args) => withNotionResilience(() => client.databases.update(args)),
    },
    pages: {
      create: (args) => withNotionResilience(() => client.pages.create(args)),
      update: (args) => withNotionResilience(() => client.pages.update(args)),
    },
  };
}
