// Local line/word boxes for the page scans, via Apple's Vision framework (no cloud, no LLM). Contract §5.
// Usage: swift scripts/ocr_boxes.swift <data/pages dir> <output.jsonl> [post_id,post_id,...]
// The optional third argument restricts the run to those letters (default: every numeric directory).
// One JSON row per page: {post_id, page, width, height, engine, created_at, lines:[{text, confidence, box, words}]}.
// Boxes are [x0, y0, x1, y1] in source-page pixels with the origin at the TOP-left (Vision's own origin is
// bottom-left; it is flipped here). Any page that cannot be read aborts the run (loud failure).
import Foundation
import Vision
import ImageIO

func die(_ msg: String) -> Never {
    FileHandle.standardError.write(Data("FAILED: \(msg)\n".utf8))
    exit(1)
}

let args = CommandLine.arguments
if args.count < 3 || args.count > 4 { die("usage: ocr_boxes.swift <pages dir> <output.jsonl> [post_id,...]") }
let only: Set<Int>? = args.count == 4 ? Set(args[3].split(separator: ",").compactMap { Int($0) }) : nil
let pagesDir = URL(fileURLWithPath: args[1])
let fm = FileManager.default
guard let letters = try? fm.contentsOfDirectory(atPath: pagesDir.path) else { die("cannot list \(pagesDir.path)") }

let iso = ISO8601DateFormatter()
var out = ""
var nPages = 0
for letter in letters.sorted() {
    guard let postId = Int(letter), only == nil || only!.contains(postId) else { continue }
    let dir = pagesDir.appendingPathComponent(letter)
    let files = ((try? fm.contentsOfDirectory(atPath: dir.path)) ?? []).filter { $0.hasPrefix("page_") && $0.hasSuffix(".png") }.sorted()
    if files.isEmpty { die("no page_NNN.png in \(dir.path)") }
    for f in files {
        guard let page = Int(f.dropFirst(5).dropLast(4)) else { die("bad page file name \(f)") }
        let url = dir.appendingPathComponent(f)
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { die("cannot decode \(url.path)") }
        let W = Double(img.width), H = Double(img.height)
        let req = VNRecognizeTextRequest()
        req.recognitionLevel = .accurate
        req.usesLanguageCorrection = false
        req.recognitionLanguages = ["en-US"]
        do { try VNImageRequestHandler(cgImage: img, options: [:]).perform([req]) }
        catch { die("Vision failed on \(url.path): \(error)") }
        // Vision normalized rect (origin bottom-left) -> pixel [x0,y0,x1,y1] (origin top-left).
        // Vision can return rects a few pixels outside the image; they are clamped to the page.
        func px(_ r: CGRect) -> [Int] {
            func c(_ v: Double, _ hi: Double) -> Int { return Int(min(max(v, 0), hi).rounded()) }
            return [c(r.minX * W, W), c((1 - r.maxY) * H, H), c(r.maxX * W, W), c((1 - r.minY) * H, H)]
        }
        var lines: [[String: Any]] = []
        for obs in (req.results ?? []) {
            guard let cand = obs.topCandidates(1).first else { continue }
            var words: [[String: Any]] = []
            let s = cand.string
            s.enumerateSubstrings(in: s.startIndex..<s.endIndex, options: .byWords) { w, range, _, _ in
                if let w = w, let b = try? cand.boundingBox(for: range) { words.append(["text": w, "box": px(b.boundingBox)]) }
            }
            lines.append(["text": s, "confidence": Double(cand.confidence), "box": px(obs.boundingBox), "words": words])
        }
        // Reading order: top to bottom, then left to right.
        lines.sort { a, b in
            let ba = a["box"] as! [Int], bb = b["box"] as! [Int]
            return ba[1] != bb[1] ? ba[1] < bb[1] : ba[0] < bb[0]
        }
        let row: [String: Any] = ["post_id": postId, "page": page, "width": img.width, "height": img.height,
                                  "engine": "apple-vision VNRecognizeTextRequest accurate, language correction off",
                                  "created_at": iso.string(from: Date()), "lines": lines]
        guard let data = try? JSONSerialization.data(withJSONObject: row, options: [.sortedKeys]),
              let js = String(data: data, encoding: .utf8) else { die("cannot serialize \(url.path)") }
        out += js + "\n"
        nPages += 1
        FileHandle.standardError.write(Data("\(postId) p\(page): \(lines.count) lines\n".utf8))
    }
}
do { try out.write(toFile: args[2], atomically: true, encoding: .utf8) } catch { die("cannot write \(args[2]): \(error)") }
print("wrote \(nPages) pages to \(args[2])")
