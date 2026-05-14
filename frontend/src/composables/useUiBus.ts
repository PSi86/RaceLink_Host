// Tiny in-memory event bus for cross-component UI signals (header → page,
// page → modal). VueUse's createGlobalState would also work, but we have
// just a couple of signals — a plain ref-based singleton keeps the import
// graph flat and the surface predictable.
//
// Each signal is an integer counter that increments on every request.
// Watchers fire on every increment, so a request always opens its dialog
// even when the dialog was just closed by the operator.

import { ref } from 'vue'

const discoverRequest = ref(0)
const resyncRequest = ref(0)
const newGroupRequest = ref(0)
const rlPresetsRequest = ref(0)
const wledPresetsRequest = ref(0)
const fwUpdateRequest = ref(0)

export function useUiBus() {
  return {
    discoverRequest,
    resyncRequest,
    newGroupRequest,
    rlPresetsRequest,
    wledPresetsRequest,
    fwUpdateRequest,
    requestDiscover() {
      discoverRequest.value += 1
    },
    requestResync() {
      resyncRequest.value += 1
    },
    requestNewGroup() {
      newGroupRequest.value += 1
    },
    requestRlPresets() {
      rlPresetsRequest.value += 1
    },
    requestWledPresets() {
      wledPresetsRequest.value += 1
    },
    requestFwUpdate() {
      fwUpdateRequest.value += 1
    },
  }
}
