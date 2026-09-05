import { clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn/ui's class helper: conditional classes, with later Tailwind
 *  utilities correctly overriding earlier conflicting ones. */
export function cn(...inputs) {
  return twMerge(clsx(inputs));
}
