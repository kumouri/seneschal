/**
 * Anthropic-backed implementation of the `LlmClient` interface used by the
 * stage-4 screening conversation. Uses plain `fetch` so it runs unchanged in
 * the Cloudflare Workers runtime. The response parsing is split out as a pure
 * function so it can be unit-tested without a network call or API key.
 */
import type { LlmClient, LlmResult, LlmTurn } from "./brain";
import type { ToolSchema } from "./prompt";

interface AnthropicContentBlock {
  type: string;
  text?: string;
  name?: string;
  input?: Record<string, unknown>;
}

interface AnthropicResponse {
  content?: AnthropicContentBlock[];
}

/** Parse an Anthropic `/v1/messages` response into our `LlmResult`. Pure. */
export function parseAnthropicResponse(data: AnthropicResponse): LlmResult {
  const blocks = data.content ?? [];
  const toolBlock = blocks.find((b) => b.type === "tool_use");
  if (toolBlock !== undefined && typeof toolBlock.name === "string") {
    return { toolUse: { name: toolBlock.name, input: toolBlock.input ?? {} } };
  }
  const text = blocks
    .filter((b) => b.type === "text" && typeof b.text === "string")
    .map((b) => b.text as string)
    .join("");
  return { text };
}

export interface AnthropicClientOptions {
  apiKey: string;
  /** e.g. "claude-haiku-4-5-20251001" — fast + cheap for real-time turns. */
  model: string;
  maxTokens?: number;
}

export function createAnthropicClient(opts: AnthropicClientOptions): LlmClient {
  return {
    async respond(system: string, tools: ToolSchema[], history: LlmTurn[]): Promise<LlmResult> {
      const res = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: {
          "x-api-key": opts.apiKey,
          "anthropic-version": "2023-06-01",
          "content-type": "application/json",
        },
        body: JSON.stringify({
          model: opts.model,
          max_tokens: opts.maxTokens ?? 256,
          system,
          // Our ToolSchema already matches Anthropic's tool format.
          tools,
          messages: history.map((t) => ({ role: t.role, content: t.content })),
        }),
      });
      if (!res.ok) {
        throw new Error(`Anthropic API ${res.status}: ${await res.text()}`);
      }
      return parseAnthropicResponse((await res.json()) as AnthropicResponse);
    },
  };
}
