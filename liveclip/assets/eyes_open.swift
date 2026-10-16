// 用 macOS Vision 检测图里每张脸的眼睛睁开程度，选封面帧用。
// 用法: eyes_open 图1.png 图2.png ...   每张图输出一行 JSON: {"file":..., "faces":N, "min_eye":最小睁眼度, "eyes":[...], "boxes":[[x,y,w,h],...]}
// boxes 是所有检测到的脸框（含没有眼睛关键点的侧脸、半张脸），按图宽高归一化，原点在左上角；liveclip.qa 用它查切脸、字幕挡脸。
// 睁眼度 = 眼睛关键点的高/宽比，闭眼约 <0.12，正常睁开约 0.2–0.35。
import Foundation
import Vision
import AppKit

func eyeOpenness(_ pts: [CGPoint]) -> Double {
    let xs = pts.map { $0.x }, ys = pts.map { $0.y }
    let w = (xs.max()! - xs.min()!), h = (ys.max()! - ys.min()!)
    return w > 0 ? Double(h / w) : 0
}
for path in CommandLine.arguments.dropFirst() {
    guard let img = NSImage(contentsOfFile: path), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        print("{\"file\":\"\(path)\",\"faces\":0,\"min_eye\":0,\"eyes\":[],\"boxes\":[]}"); continue
    }
    let req = VNDetectFaceLandmarksRequest()
    try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
    var eyes: [Double] = []
    var boxes: [String] = []
    for f in (req.results ?? []) {
        let b = f.boundingBox   // Vision 原点在左下角
        boxes.append(String(format: "[%.4f,%.4f,%.4f,%.4f]", b.origin.x, 1 - b.origin.y - b.height, b.width, b.height))
        guard let lm = f.landmarks, let l = lm.leftEye, let r = lm.rightEye else { continue }
        // 眼睛坐标按脸框归一化，直接算高宽比即可
        eyes.append(min(eyeOpenness(l.normalizedPoints), eyeOpenness(r.normalizedPoints)))
    }
    let minEye = eyes.min() ?? 0
    let es = eyes.map { String(format: "%.3f", $0) }.joined(separator: ",")
    print("{\"file\":\"\(path)\",\"faces\":\(eyes.count),\"min_eye\":\(String(format: "%.3f", minEye)),\"eyes\":[\(es)],\"boxes\":[\(boxes.joined(separator: ","))]}")
}
