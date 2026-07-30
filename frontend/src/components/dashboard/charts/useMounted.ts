import React from "react";

/** True shortly after mount — lets bars/meters animate their width up from 0 on first render. */
export function useMounted(delay = 60): boolean {
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => {
    const t = setTimeout(() => setMounted(true), delay);
    return () => clearTimeout(t);
  }, [delay]);
  return mounted;
}
