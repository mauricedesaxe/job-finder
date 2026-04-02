import OpenAI from "openai";

// Singleton: the first apiKey wins for the lifetime of the process.
// This is fine for single-key batch runs but would need a Map<key, client>
// if we ever need multiple API keys in the same process.
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
