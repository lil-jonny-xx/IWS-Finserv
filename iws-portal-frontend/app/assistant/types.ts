// Shared types for the Jarvis assistant UI.
// Mirrors the backend contract in mis-portal/assistant/{engine,persistence}.py.

export interface User {
  role: string;
  full_name: string;
  entity_id?: number;
}

export interface Entity {
  id: number;
  name: string;
  pan_group?: string;
}

export interface Citation {
  url: string;
  title?: string;
}

export interface Message {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  tool_calls?: string[] | null;
  citations?: Citation[] | null;
  created_at: string;
}

export interface Conversation {
  id: number;
  user_id: number;
  title: string | null;
  scope_entity_id: number | null;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

export interface ConversationDetail extends Conversation {
  messages: Message[];
}

// Streaming events emitted by POST /api/v1/assistant/chat (SSE).
export type AssistantEvent =
  | { type: 'text'; text: string }
  | { type: 'tool'; name: string }
  | { type: 'citations'; items: Citation[] }
  | { type: 'done'; content: string; citations: Citation[]; tool_names: string[] }
  | { type: 'error'; message: string };
