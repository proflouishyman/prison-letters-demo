// Local line/word boxes for the page scans, via Apple's Vision framework (no cloud, no LLM). Contract §5.
// Usage: swift scripts/ocr_boxes.swift <data/pages dir> <output.jsonl> [post_id,post_id,...]
// The optional third argument restricts the run to those letters (default: every numeric directory).
// Each page is read as overlapping full-width strips (Vision drops lines on whole pages); see Contract §5.
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

// Strip geometry (px) and dedupe thresholds. See contract §5.
let bandPx = 500.0, stepPx = 350.0
let edgeTolPx = 3          // a box this close to an interior strip edge counts as cut by it
let iouMin = 0.5           // same line if boxes overlap at least this much
let insideMin = 0.7        // ...or if the smaller box is at least this much inside the other and the texts are similar
let textSimMin = 0.6       // normalized edit similarity, or substring containment, counts as "similar"

struct Cand { let text: String; let conf: Double; let box: [Int]; let words: [[String: Any]]; let cut: Bool }

func area(_ b: [Int]) -> Double { return Double(max(0, b[2] - b[0]) * max(0, b[3] - b[1])) }
func inter(_ a: [Int], _ b: [Int]) -> Double {
    return Double(max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1])))
}
func norm(_ s: String) -> [Character] { return Array(s.lowercased().filter { $0.isLetter || $0.isNumber }) }
func similar(_ s: String, _ t: String) -> Bool {
    let a = norm(s), b = norm(t)
    if a.isEmpty || b.isEmpty { return a.isEmpty && b.isEmpty }
    let (sh, lo) = a.count <= b.count ? (a, b) : (b, a)
    if String(lo).contains(String(sh)) { return true }
    var prev = Array(0...lo.count)
    for i in 1...sh.count {
        var cur = [i] + Array(repeating: 0, count: lo.count)
        for j in 1...lo.count { cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (sh[i - 1] == lo[j - 1] ? 0 : 1)) }
        prev = cur
    }
    return 1.0 - Double(prev[lo.count]) / Double(lo.count) >= textSimMin
}
func sameLine(_ a: Cand, _ b: Cand) -> Bool {
    let i = inter(a.box, b.box)
    let u = area(a.box) + area(b.box) - i
    if u > 0 && i / u >= iouMin { return true }
    let small = min(area(a.box), area(b.box))
    return small > 0 && i / small >= insideMin && similar(a.text, b.text)
}
/// Merge the strips' candidates into one list of line dicts. Order of preference: lines not cut by a strip edge,
/// then longer text, then higher confidence; a cut line survives only when no preferred candidate matches it.
func dedupe(_ cands: [Cand]) -> [[String: Any]] {
    let order = cands.sorted { a, b in
        if a.cut != b.cut { return !a.cut }
        if a.text.count != b.text.count { return a.text.count > b.text.count }
        return a.conf > b.conf
    }
    var kept: [Cand] = []
    for c in order where !kept.contains(where: { sameLine($0, c) }) { kept.append(c) }
    return kept.map { ["text": $0.text, "confidence": $0.conf, "box": $0.box, "words": $0.words] }
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
        // Vision silently drops lines when run on a whole page (136440 p1: 9 of ~25 lines), so the page is read as
        // overlapping full-width strips (regionOfInterest) and the strips' lines are deduplicated afterwards.
        var cands: [Cand] = []
        // The whole-page pass is kept as one more candidate source: it finds some lines the strips miss.
        var y = 0.0, strip = -1
        while true {
            let h = strip < 0 ? H : min(bandPx, H - y)
            let req = VNRecognizeTextRequest()
            req.recognitionLevel = .accurate
            req.usesLanguageCorrection = false
            req.recognitionLanguages = ["en-US"]
            // ROI rects are normalized with the origin at the bottom-left.
            req.regionOfInterest = CGRect(x: 0, y: 1 - (y + h) / H, width: 1, height: h / H)
            do { try VNImageRequestHandler(cgImage: img, options: [:]).perform([req]) }
            catch { die("Vision failed on \(url.path) strip \(strip): \(error)") }
            // Observation and word rects are normalized to the ROI (bottom-left origin), not the page:
            // map them back to page pixels with the top-left origin.
            func px(_ r: CGRect) -> [Int] {
                func c(_ v: Double, _ hi: Double) -> Int { return Int(min(max(v, 0), hi).rounded()) }
                return [c(r.minX * W, W), c(y + (1 - r.maxY) * h, H), c(r.maxX * W, W), c(y + (1 - r.minY) * h, H)]
            }
            for obs in (req.results ?? []) {
                guard let cand = obs.topCandidates(1).first else { continue }
                var words: [[String: Any]] = []
                let s = cand.string
                s.enumerateSubstrings(in: s.startIndex..<s.endIndex, options: .byWords) { w, range, _, _ in
                    if let w = w, let b = try? cand.boundingBox(for: range) { words.append(["text": w, "box": px(b.boundingBox)]) }
                }
                let box = px(obs.boundingBox)
                // A box touching an interior strip edge may be a line cut in two by that edge.
                let cut = (y > 0 && box[1] <= Int(y) + edgeTolPx) || (y + h < H && box[3] >= Int(y + h) - edgeTolPx)
                cands.append(Cand(text: s, conf: Double(cand.confidence), box: box, words: words, cut: cut))
            }
            strip += 1
            if strip == 0 { y = 0; continue }
            if y + h >= H { break }
            y += stepPx
        }
        var lines = dedupe(cands)

        // Reading order: top to bottom, then left to right.
        lines.sort { a, b in
            let ba = a["box"] as! [Int], bb = b["box"] as! [Int]
            return ba[1] != bb[1] ? ba[1] < bb[1] : ba[0] < bb[0]
        }
        let row: [String: Any] = ["post_id": postId, "page": page, "width": img.width, "height": img.height,
                                  "engine": "apple-vision VNRecognizeTextRequest accurate, language correction off, whole page + strips band=\(Int(bandPx))px step=\(Int(stepPx))px, deduped IoU>=\(iouMin)",
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
