/* Один владелец управления на страницу: чат и массовый перенос при обновлении используют тот же id. */
export const WINDOW = crypto.randomUUID().replaceAll('-', '');
