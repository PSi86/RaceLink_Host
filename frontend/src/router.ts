import { createRouter, createWebHistory } from 'vue-router'

import DevicesPage from '@/pages/DevicesPage.vue'
import { getBasePath } from '@/api/client'

// Vue Router runs on top of the Flask blueprint's URL prefix. Both
// ``/racelink/`` and ``/racelink/scenes`` are server-rendered as the same
// SPA shell, so a hard reload on either path enters the SPA at the right
// route automatically.
//
// Slice 12 — route-level code-splitting. ``DevicesPage`` is statically
// imported because it's the landing route (operators hit ``/`` first)
// and shipping it in the initial bundle keeps first-paint fast.
// ``ScenesPage`` is the heavy one (it pulls in vuedraggable + Sortable.js
// + the offset_group editor + the scene-action body widgets — about
// 50 kB gzip), so its chunk is lazy-loaded the first time the operator
// navigates to ``/scenes``. Pure-Devices operators never download it.
export const router = createRouter({
  history: createWebHistory(getBasePath() || '/'),
  routes: [
    { path: '/', name: 'devices', component: DevicesPage },
    {
      path: '/scenes',
      name: 'scenes',
      component: () => import('@/pages/ScenesPage.vue'),
    },
  ],
})
