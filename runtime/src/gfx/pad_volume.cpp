// SPDX-License-Identifier: GPL-3.0-or-later
// Declarations and the reason for it in pad_volume.h. Core Audio, by endpoint id: no COM pointer is kept between calls, so
// the calls need not come from one thread.
#include "pad_volume.h"

#include <windows.h>

#include <endpointvolume.h>
#include <mmdeviceapi.h>
#include <propsys.h>

#include <mutex>
#include <string>
#include <utility>
#include <vector>

#include "nxrt/log.h"

namespace nxrt::gfx::pad_volume {
namespace {

constexpr const char* kTag = "haptic";
// PKEY_Device_FriendlyName, spelled out so that no property key library is needed
constexpr PROPERTYKEY kFriendlyName = {{0xa45c254e, 0xdf1c, 0x4efd, {0x80, 0x20, 0x67, 0xd1, 0x46, 0xa8, 0x50, 0xe0}}, 14};

struct Saved {
    std::wstring id;
    float volume = 1.f;
    BOOL mute = FALSE;
};

std::mutex g_mu;                 // g_saved, and one change at a time
std::vector<Saved> g_saved;

template <typename T>
struct Ref {
    T* p = nullptr;
    ~Ref() {
        if (p) p->Release();
    }
    T* operator->() const { return p; }
    void** out() { return reinterpret_cast<void**>(&p); }
};

// COM for the calling thread, whatever apartment it already has.
struct Com {
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    ~Com() {
        if (SUCCEEDED(hr)) CoUninitialize();
    }
};

bool enumerator(Ref<IMMDeviceEnumerator>& e) {
    return SUCCEEDED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, __uuidof(IMMDeviceEnumerator), e.out()));
}

bool volume_of(IMMDevice* device, Ref<IAudioEndpointVolume>& v) {
    return SUCCEEDED(device->Activate(__uuidof(IAudioEndpointVolume), CLSCTX_ALL, nullptr, v.out()));
}

std::wstring name_of(IMMDevice* device) {
    Ref<IPropertyStore> props;
    if (FAILED(device->OpenPropertyStore(STGM_READ, &props.p))) return {};
    PROPVARIANT v;
    PropVariantInit(&v);
    std::wstring name;
    if (SUCCEEDED(props->GetValue(kFriendlyName, &v)) && v.vt == VT_LPWSTR && v.pwszVal) name = v.pwszVal;
    PropVariantClear(&v);
    return name;
}

std::wstring widen(const char* s) {
    const int n = MultiByteToWideChar(CP_UTF8, 0, s, -1, nullptr, 0);
    if (n <= 1) return {};
    std::wstring w(static_cast<size_t>(n - 1), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s, -1, w.data(), n);
    return w;
}

}  // namespace

bool raise(const char* endpoint_name) {
    if (!endpoint_name || !*endpoint_name) return false;
    const std::wstring wanted = widen(endpoint_name);
    std::lock_guard<std::mutex> lk(g_mu);
    const Com com;
    Ref<IMMDeviceEnumerator> devices;
    Ref<IMMDeviceCollection> list;
    UINT count = 0;
    if (!enumerator(devices) || FAILED(devices->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &list.p)) || FAILED(list->GetCount(&count)))
        return false;
    for (UINT i = 0; i < count; ++i) {
        Ref<IMMDevice> device;
        Ref<IAudioEndpointVolume> v;
        if (FAILED(list->Item(i, &device.p)) || name_of(device.p) != wanted || !volume_of(device.p, v)) continue;
        Saved now;
        if (FAILED(v->GetMasterVolumeLevelScalar(&now.volume)) || FAILED(v->GetMute(&now.mute))) continue;
        if (now.volume >= kFullVolume && !now.mute) return false;   // nothing to change (and a value saved earlier stays)
        LPWSTR id = nullptr;
        if (FAILED(device->GetId(&id)) || !id) continue;
        now.id = id;
        CoTaskMemFree(id);
        if (FAILED(v->SetMasterVolumeLevelScalar(1.f, nullptr))) return false;
        v->SetMute(FALSE, nullptr);
        std::erase_if(g_saved, [&](const Saved& s) { return s.id == now.id; });
        g_saved.push_back(now);
        NXLOG_I(kTag, "%s: volume raised from %.0f%%%s to 100%%", endpoint_name, static_cast<double>(now.volume) * 100.0,
                now.mute ? " (muted)" : "");
        return true;
    }
    return false;
}

void restore() {
    std::lock_guard<std::mutex> lk(g_mu);
    if (g_saved.empty()) return;
    const Com com;
    Ref<IMMDeviceEnumerator> devices;
    if (!enumerator(devices)) return;
    std::erase_if(g_saved, [&](const Saved& s) {
        Ref<IMMDevice> device;
        Ref<IAudioEndpointVolume> v;
        float now = 0.f;
        BOOL muted = FALSE;
        if (FAILED(devices->GetDevice(s.id.c_str(), &device.p)) || !volume_of(device.p, v) || FAILED(v->GetMasterVolumeLevelScalar(&now)) ||
            FAILED(v->GetMute(&muted)))
            return false;                           // gone for now: keep it
        const PutBack back = put_back(now, muted != FALSE);
        if (back.volume && FAILED(v->SetMasterVolumeLevelScalar(s.volume, nullptr))) return false;
        if (back.mute && FAILED(v->SetMute(s.mute, nullptr))) return false;
        if (back.volume) NXLOG_I(kTag, "controller volume put back to %.0f%%", static_cast<double>(s.volume) * 100.0);
        return true;
    });
}

bool raised() {
    std::lock_guard<std::mutex> lk(g_mu);
    return !g_saved.empty();
}

}  // namespace nxrt::gfx::pad_volume
