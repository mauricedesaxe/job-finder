import OpenAI from "openai";

let client: OpenAI | null = null;

export function getClient(apiKey: string): OpenAI {
  if (!client) {
    client = new OpenAI({
      apiKey,
      baseURL: "https://openrouter.ai/api/v1",
      maxRetries: 0,
    });
  }
  return client;
}
