// Standard shadcn-vue ``cn()`` helper. Combines clsx's conditional-class
// composition with tailwind-merge's deduplication of conflicting Tailwind
// utility classes (e.g. ``cn('px-2', 'px-4') === 'px-4'``). Required by
// every component under ``components/ui/`` so that variant overrides at
// the call site replace the component's defaults instead of stacking.

import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}
