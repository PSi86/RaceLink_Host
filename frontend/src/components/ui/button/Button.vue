<script setup lang="ts">
// shadcn-vue style Button. Variants are minimal for now (default,
// secondary, destructive, ghost) — enough for dialog actions. New
// variants land here as new dialogs need them.

import { computed } from 'vue'
import { Primitive, type PrimitiveProps } from 'reka-ui'
import { cva, type VariantProps } from 'class-variance-authority'

import { cn } from '@/lib/utils'

const buttonVariants = cva(
  'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium ' +
    'transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 ' +
    'focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-50',
  {
    variants: {
      variant: {
        default: 'bg-primary text-primary-foreground hover:bg-primary/90',
        secondary: 'bg-secondary text-secondary-foreground hover:bg-secondary/80',
        // ``destructive`` is the pink-outline mirror of ``brand`` —
        // same outline + tinted-fill structure but in brand pink.
        // Used on Delete buttons and the confirm CTA of destructive
        // confirm dialogs. Previously this slot held a solid red
        // ``bg-destructive`` fill; the outline form fits the new
        // outline-language of brand/run without giving up the
        // danger-signal of pink.
        destructive:
          'border border-brand-pink/55 bg-brand-pink/10 text-brand-pink font-display tracking-wide ' +
          'hover:bg-brand-pink/20 hover:border-brand-pink/85',
        ghost: 'hover:bg-secondary hover:text-secondary-foreground',
        outline: 'border border-border bg-background hover:bg-secondary hover:text-secondary-foreground',
        // ``brand`` mirrors the .btn-brand utility class used on the
        // AppHeader's Save button: a quiet cyan-outline CTA that reads
        // as the primary action of a dialog without shouting. The
        // brand cyan stays neon here because it sits as text/border on
        // a near-transparent fill, not as a saturated background.
        brand:
          'border border-brand-cyan/45 bg-brand-cyan/10 text-brand-cyan font-display tracking-wide ' +
          'hover:bg-brand-cyan/20 hover:border-brand-cyan/70',
        // ``run`` is the louder sibling of ``brand`` — for buttons
        // that *execute* an action (Start, Re-sync, Send, Start
        // update) rather than commit state. Cyan border keeps it
        // visually separate from destructive (pink) while the bumped
        // pink→cyan gradient fill (--gradient-run) gives the punchy
        // action-button feel.
        run:
          'btn-run-bg border border-brand-cyan/55 text-foreground font-display tracking-wide ' +
          'hover:border-brand-cyan/85',
      },
      size: {
        default: 'h-9 px-4 py-2',
        sm: 'h-8 px-3 text-xs',
        lg: 'h-10 px-6',
        icon: 'h-9 w-9',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  },
)

export type ButtonVariants = VariantProps<typeof buttonVariants>

interface Props extends PrimitiveProps {
  variant?: ButtonVariants['variant']
  size?: ButtonVariants['size']
  class?: string
  type?: 'button' | 'submit' | 'reset'
  disabled?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  as: 'button',
  type: 'button',
})

const classes = computed(() =>
  cn(buttonVariants({ variant: props.variant, size: props.size }), props.class),
)
</script>

<template>
  <Primitive :as="as" :as-child="asChild" :type="type" :disabled="disabled" :class="classes">
    <slot />
  </Primitive>
</template>
