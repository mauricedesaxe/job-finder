import type { JobListing } from "../../types";

export function buildNotionProperties(job: JobListing) {
  return {
    "Job Title": {
      title: [{ text: { content: job.title } }],
    },
    Company: {
      rich_text: [{ text: { content: job.company } }],
    },
    URL: {
      url: job.url,
    },
    Source: {
      select: { name: job.source },
    },
    Keywords: {
      multi_select: job.keywordsMatched.map((k) => ({ name: k })),
    },
    "Date Scraped": {
      date: { start: job.dateScraped },
    },
    ...(job.datePosted
      ? {
          "Date Posted": {
            date: { start: job.datePosted },
          },
        }
      : {}),
    ...(job.location
      ? {
          Location: {
            rich_text: [{ text: { content: job.location } }],
          },
        }
      : {}),
    Status: {
      select: { name: "To Review" },
    },
  };
}

export function descriptionToBlocks(description: string) {
  if (!description) return [];

  // Split on double newlines to preserve paragraph structure
  const paragraphs = description
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter(Boolean);

  const blocks: Array<{
    object: "block";
    type: "paragraph";
    paragraph: {
      rich_text: Array<{ type: "text"; text: { content: string } }>;
    };
  }> = [];

  for (const paragraph of paragraphs) {
    // Notion limits rich_text content to 2000 chars per block
    if (paragraph.length <= 2000) {
      blocks.push({
        object: "block",
        type: "paragraph",
        paragraph: {
          rich_text: [{ type: "text", text: { content: paragraph } }],
        },
      });
    } else {
      // Chunk oversized paragraphs
      for (let i = 0; i < paragraph.length; i += 2000) {
        blocks.push({
          object: "block",
          type: "paragraph",
          paragraph: {
            rich_text: [{ type: "text", text: { content: paragraph.slice(i, i + 2000) } }],
          },
        });
      }
    }
  }

  return blocks.slice(0, 100);
}
