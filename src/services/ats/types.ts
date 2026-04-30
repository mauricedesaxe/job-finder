import { z } from "zod";

export type AtsSource = "lever" | "ashby" | "greenhouse";

export type WorkplaceType = "Remote" | "Hybrid" | "OnSite";

// Injected so unit tests can supply recorded responses without hitting the network.
export type Fetcher = (url: string) => Promise<Response>;

export interface AtsJobData {
  source: AtsSource;
  location: string;
  locations: string[];
  workplaceType: WorkplaceType | null;
  country: string | null;
}

// All three ATS surfaces emit `null` for unset fields on at least some payloads
// (observed live on Ashby), so optional fields use .nullish() to accept both
// undefined (missing) and null (explicitly null).

// Lever — https://api.lever.co/v0/postings/{org}/{id}?mode=json
export const leverJobSchema = z.object({
  categories: z
    .object({
      location: z.string().nullish(),
      allLocations: z.array(z.string()).nullish(),
      commitment: z.string().nullish(),
      department: z.string().nullish(),
      team: z.string().nullish(),
    })
    .nullish(),
  workplaceType: z.string().nullish(),
  country: z.string().nullish(),
});

export type LeverJob = z.infer<typeof leverJobSchema>;

// Ashby — https://api.ashbyhq.com/posting-api/job-board/{org}
export const ashbyJobSchema = z.object({
  id: z.string(),
  location: z.string().nullish(),
  secondaryLocations: z
    .array(
      z.object({
        location: z.string(),
      }),
    )
    .nullish(),
  isRemote: z.boolean().nullish(),
  workplaceType: z.string().nullish(),
  address: z
    .object({
      postalAddress: z
        .object({
          addressCountry: z.string().nullish(),
          addressLocality: z.string().nullish(),
        })
        .nullish(),
    })
    .nullish(),
});

export const ashbyOrgResponseSchema = z.object({
  jobs: z.array(ashbyJobSchema),
  apiVersion: z.string().nullish(),
});

export type AshbyJob = z.infer<typeof ashbyJobSchema>;
export type AshbyOrgResponse = z.infer<typeof ashbyOrgResponseSchema>;

// Greenhouse — https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{id}
export const greenhouseJobSchema = z.object({
  id: z.number(),
  location: z
    .object({
      name: z.string(),
    })
    .nullish(),
  offices: z
    .array(
      z.object({
        id: z.number(),
        name: z.string().nullish(),
        location: z.string().nullish(),
      }),
    )
    .nullish(),
});

export type GreenhouseJob = z.infer<typeof greenhouseJobSchema>;
