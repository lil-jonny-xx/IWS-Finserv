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

// Chart specs emitted by the backend render_chart tool (validated server-side).
export type ChartSpec =
  | { chart_type: 'donut' | 'bar'; title?: string; unit?: string; series: { label: string; value: number }[] }
  | { chart_type: 'line'; title?: string; unit?: string; points: { x: string; y: number }[] }
  | { chart_type: 'heatmap'; title?: string; labels: string[]; matrix: (number | null)[][] };

export interface Message {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  tool_calls?: string[] | null;
  citations?: Citation[] | null;
  charts?: ChartSpec[] | null;
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
  | { type: 'chart'; spec: ChartSpec }
  | { type: 'citations'; items: Citation[] }
  | { type: 'done'; content: string; citations: Citation[]; tool_names: string[]; charts: ChartSpec[] }
  | { type: 'error'; message: string };
