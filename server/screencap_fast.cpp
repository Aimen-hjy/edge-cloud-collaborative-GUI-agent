// 低分辨率抓屏：读 screencap 的原始 RGBA 流 → 下采样 → 输出 JPEG。
//
// 用法：screencap | screencap_fast <width> <height> [max_side=1024]
//   width/height 是屏幕物理分辨率（adb shell wm size 可得），用于解析原始流。
//   原始流是 RGBA8888 紧打包、无头（Android 6+ screencap）；若带 12 字节头
//   （老 screencap: w,h,format 各 4 字节小端），本工具会自动识别跳过。
// 输出：stdout 直接写 JPEG（长边 max_side 的小图，质量 80），供云端 VL / 截图指纹使用。
//
// 为什么用 JPEG 不用 PNG：截图像素是照片/图标混合，PNG 几乎压不动（683×1024 要
// ~840KB），而 JPEG 质量 80 只要 ~150KB，adb 传输和云端上传都快一个量级；
// 下游统一走 PIL Image.open（PNG/JPEG 都能解），8×8 灰度指纹对 JPEG 噪声不敏感。
//
// 为什么快：screencap 不带 -p 时只读帧缓冲、不做 PNG 编码（那 ~0.5s 是全分辨率
// libpng 编码，白费），原始 RGBA 直接流进本工具，在内存里先 area-average 下采样
// 再编一张小 JPEG，编码量从 1600×2400 降到 ~683×1024，快一个量级。
//
// 构建（NDK r27c，Windows 宿主）：
//   aarch64-linux-android23-clang++ -O2 -s -std=c++17 -static-libstdc++ \
//       -static-libgcc -I /d/code/MNN/3rd_party/imageHelper screencap_fast.cpp -o screencap_fast
// 注意：不能用 -static（Bionic 报 TLS 段对齐错）也不能 -static-pie（运行时段错误），
// 必须「静态链 libc++/libgcc + 动态链 Bionic libc」，见 stb 的 stbi_write_png_to_func。

#define STB_IMAGE_WRITE_IMPLEMENTATION
#include "stb_image_write.h"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>
#include <unistd.h>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

static void write_stdout(void* /*ctx*/, void* data, int size) {
    fwrite(data, 1, (size_t)size, stdout);
}

int main(int argc, char** argv) {
#ifdef _WIN32
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
#endif
    if (argc < 3) {
        fprintf(stderr, "usage: screencap_fast <width> <height> [max_side=1024]\n");
        return 2;
    }
    int W = atoi(argv[1]);
    int H = atoi(argv[2]);
    int max_side = argc >= 4 ? atoi(argv[3]) : 1024;
    if (W <= 0 || H <= 0 || max_side <= 0) {
        fprintf(stderr, "invalid args\n");
        return 2;
    }

    // 1. 读全部 stdin（screencap 的原始 RGBA 流）
    std::vector<uint8_t> data;
    uint8_t buf[1 << 16];
    ssize_t n;
    while ((n = read(0, buf, sizeof(buf))) > 0) {
        data.insert(data.end(), buf, buf + n);
    }
    const size_t raw = (size_t)W * H * 4;
    if (data.size() < raw) {
        fprintf(stderr, "input too small: got %zu, need %zu (W=%d H=%d)\n",
                data.size(), raw, W, H);
        return 1;
    }

    // 2. 定位像素起点：无头 = raw 字节；带头 = 多出的字节全在开头。
    //    screencap 头是 12（w,h,format）或 16（+colorspace）字节，off 应 ≤16；
    //    更大说明命令行 W/H 与 screencap 实际分辨率不符，报错交给上层回退。
    size_t off = 0;
    if (data.size() > raw) {
        off = data.size() - raw;
        if (off > 16) {
            fprintf(stderr, "resolution mismatch: extra %zu bytes, wm size=%dx%d\n",
                    off, W, H);
            return 1;
        }
    }
    const uint8_t* src = &data[off];

    // 3. 计算目标尺寸（长边 = max_side，保持比例）
    int nw = W, nh = H;
    if (W >= H) {
        nw = max_side;
        nh = (int)((long long)H * max_side / W);
        if (nh < 1) nh = 1;
    } else {
        nh = max_side;
        nw = (int)((long long)W * max_side / H);
        if (nw < 1) nw = 1;
    }

    // 4. area-average 下采样（盒式平均，保留文字/图标的可读性）
    std::vector<uint8_t> out((size_t)nw * nh * 4);
    for (int dy = 0; dy < nh; dy++) {
        int y0 = (int)((long long)dy * H / nh);
        int y1 = (int)((long long)(dy + 1) * H / nh);
        if (y1 <= y0) y1 = y0 + 1;
        for (int dx = 0; dx < nw; dx++) {
            int x0 = (int)((long long)dx * W / nw);
            int x1 = (int)((long long)(dx + 1) * W / nw);
            if (x1 <= x0) x1 = x0 + 1;
            uint64_t r = 0, g = 0, b = 0;
            int cnt = 0;
            for (int sy = y0; sy < y1; sy++) {
                const uint8_t* row = src + (size_t)sy * W * 4;
                for (int sx = x0; sx < x1; sx++) {
                    const uint8_t* p = row + (size_t)sx * 4;
                    r += p[0];
                    g += p[1];
                    b += p[2];
                    cnt++;
                }
            }
            uint8_t* d = &out[((size_t)dy * nw + dx) * 4];
            d[0] = (uint8_t)(r / cnt);
            d[1] = (uint8_t)(g / cnt);
            d[2] = (uint8_t)(b / cnt);
            d[3] = 255;  // 截图不透明，忽略源 alpha（兼容 RGBX）
        }
    }

    // 5. 写 JPEG（质量 80）到 stdout
    if (!stbi_write_jpg_to_func(write_stdout, nullptr, nw, nh, 4, out.data(), 80)) {
        fprintf(stderr, "jpg encode failed\n");
        return 1;
    }
    fflush(stdout);
    return 0;
}
