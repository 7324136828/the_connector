export function visibleSessions(sessions, showSystemSessions) {
  return showSystemSessions
    ? sessions
    : sessions.filter((session) => session.user_session !== false);
}
