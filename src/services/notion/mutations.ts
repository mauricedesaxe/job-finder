import type { Client } from "@notionhq/client";
import type { CreatePageParameters } from "@notionhq/client/build/src/api-endpoints";
import type { JobListing, JobStatus } from "../../types";
import { buildNotionProperties, descriptionToBlocks } from "./builders";

export async function updateJobStatus(
  client: Client,
  pageId: string,
  status: JobStatus,
): Promise<void> {
  await client.pages.update({
    page_id: pageId,
    properties: {
      Status: { select: { name: status } },
    },
  });
}

export async function insertJob(
  client: Client,
  databaseId: string,
  job: JobListing,
  status: JobStatus = "To Review",
): Promise<string> {
  const properties = buildNotionProperties(job);
  properties.Status = { select: { name: status } };

  const children = job.description ? descriptionToBlocks(job.description) : [];

  const response = await client.pages.create({
    parent: { database_id: databaseId },
    properties: properties as CreatePageParameters["properties"],
    children: children as CreatePageParameters["children"],
  });

  return response.id;
}
