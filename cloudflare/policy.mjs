export const PUBLIC_FILES = ['state.json', 'live-state.json', 'ai_daily_analysis.json', 'last-update-report.json', 'provider-health.json'];
export function operationalDay(now) {
  return new Date(now - 5 * 3600000).toISOString().slice(0, 10); // 08:00 Moscow = 05:00 UTC
}
export function selectMode(now, state) {
  const day = operationalDay(now);
  const attempts = state.attemptDay === day ? (state.attempts || 0) : 0;
  if (state.publishedDay !== day && attempts < 4 && now - (state.lastGeneration || 0) >= 30 * 60000) return 'generate';
  if (state.historyDay !== day) return 'history';
  return 'live';
}
export function aiInput(body, model) {
  if (!Array.isArray(body.messages) || !body.messages.length || body.messages.length > 8) throw new Error('Invalid AI messages');
  const messages = body.messages.map(m => {
    if (!['system', 'user', 'assistant'].includes(m.role) || typeof m.content !== 'string') throw new Error('Invalid AI message');
    return { role: m.role, content: m.content };
  });
  const input = { messages, temperature: 0.05, max_tokens: Math.min(5000, Math.max(1, Number(body.max_tokens) || 5000)) };
  if (body.response_format?.type === 'json_schema') {
    input.response_format = {type:'json_schema', json_schema:body.response_format.json_schema.schema || body.response_format.json_schema};
  }
  return input;
}
