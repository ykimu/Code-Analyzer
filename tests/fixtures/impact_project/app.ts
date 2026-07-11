export function tsFunc(a: number): number {
  const b = a + 1;
  return b;
}

export function tsCaller(): number {
  return tsFunc(2);
}
