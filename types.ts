export interface JobListing {
  title: string;
  company: string;
  url: string;
  source: string;
  keywordsMatched: string[];
  datePosted: string | null;
  dateScraped: string;
}

export interface ScrapioConfig {
  keywords: string[];
  domains: string[];
  notionDatabaseId: string;
  notionToken: string;
  jinaApiKey: string;
  jinaBaseUrl: string;
  delayBetweenRequests: number;
  maxResultsPerQuery: number;
  maxPages: number;
}
