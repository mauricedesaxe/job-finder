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

// Lever — https://api.lever.co/v0/postings/{org}/{id}?mode=json
export const leverJobSchema = z.object({
  categories: z
    .object({
      location: z.string().optional(),
      allLocations: z.array(z.string()).optional(),
      commitment: z.string().optional(),
      department: z.string().optional(),
      team: z.string().optional(),
    })
    .optional(),
  workplaceType: z.string().optional(),
  country: z.string().optional(),
});

export type LeverJob = z.infer<typeof leverJobSchema>;

// Ashby — https://api.ashbyhq.com/posting-api/job-board/{org}
export const ashbyJobSchema = z.object({
  id: z.string(),
  location: z.string().optional(),
  secondaryLocations: z
    .array(
      z.object({
        location: z.string(),
      }),
    )
    .optional(),
  isRemote: z.boolean().optional(),
  workplaceType: z.string().optional(),
  address: z
    .object({
      postalAddress: z
        .object({
          addressCountry: z.string().optional(),
          addressLocality: z.string().optional(),
        })
        .optional(),
    })
    .optional(),
});

export const ashbyOrgResponseSchema = z.object({
  jobs: z.array(ashbyJobSchema),
  apiVersion: z.string().optional(),
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
    .optional(),
  offices: z
    .array(
      z.object({
        id: z.number(),
        name: z.string().optional(),
        location: z.string().optional(),
      }),
    )
    .optional(),
});

export type GreenhouseJob = z.infer<typeof greenhouseJobSchema>;
