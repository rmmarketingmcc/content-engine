// Faces and photo quality for the content engine's photo picker, on the Mac's own Vision framework (no upload).
// Usage: photo_faces <image> [<image> ...]  ->  one JSON line per image:
//   {"path": "...", "w": 4032, "h": 3024, "faces": [{"x": 0.41, "y": 0.22, "w": 0.12, "h": 0.16}], "aesthetics": 0.42, "utility": false}
// Face boxes are fractions of the image with the origin top left. "aesthetics" runs from -1 to 1 (Apple's own score),
// "utility" is true for screenshots, receipts and documents. Build: swiftc -O scripts/photo_faces.swift -o <bin>
import Foundation
import Vision
import ImageIO

func analyse(_ path: String) -> [String: Any] {
    var out: [String: Any] = ["path": path]
    let url = URL(fileURLWithPath: path)
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
        out["error"] = "unreadable"
        return out
    }
    out["w"] = img.width
    out["h"] = img.height
    let faces = VNDetectFaceRectanglesRequest()
    var requests: [VNRequest] = [faces]
    var quality: VNRequest? = nil
    if #available(macOS 15.0, *) {
        let q = VNCalculateImageAestheticsScoresRequest()
        requests.append(q)
        quality = q
    }
    let handler = VNImageRequestHandler(cgImage: img, options: [:])
    do { try handler.perform(requests) } catch { out["error"] = "\(error)"; return out }
    out["faces"] = (faces.results ?? []).map { f -> [String: Double] in
        let b = f.boundingBox  // Vision: origin bottom left
        return ["x": Double(b.minX), "y": Double(1 - b.maxY), "w": Double(b.width), "h": Double(b.height)]
    }
    if #available(macOS 15.0, *), let q = quality as? VNCalculateImageAestheticsScoresRequest,
       let r = q.results?.first {
        out["aesthetics"] = Double(r.overallScore)
        out["utility"] = r.isUtility
    }
    return out
}

for path in CommandLine.arguments.dropFirst() {
    let row = analyse(path)
    if let data = try? JSONSerialization.data(withJSONObject: row), let line = String(data: data, encoding: .utf8) {
        print(line)
    }
}
