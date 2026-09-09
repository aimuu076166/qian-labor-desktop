import Foundation
import Vision
import ImageIO
import Darwin

// Framework diagnostics never become application logs. The dedicated descriptor
// is used only for the fixed error below; image bytes/text are never written to disk.
let safeError = FileHandle(fileDescriptor: dup(STDERR_FILENO), closeOnDealloc: true)
let nullDescriptor = open("/dev/null", O_WRONLY)
if nullDescriptor >= 0 {
    dup2(nullDescriptor, STDERR_FILENO)
    close(nullDescriptor)
}

func failClosed() -> Never {
    safeError.write(Data("AI_LOCAL_REDACTION_FAILED\n".utf8))
    exit(1)
}

let maxInput = 20 * 1024 * 1024
var input = Data()
while true {
    let chunk = FileHandle.standardInput.readData(ofLength: min(65536, maxInput + 1 - input.count))
    if chunk.isEmpty { break }
    input.append(chunk)
    if input.count > maxInput { failClosed() }
}
guard !input.isEmpty,
      let source = CGImageSourceCreateWithData(input as CFData, nil),
      CGImageSourceGetCount(source) == 1,
      let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
      let width = properties[kCGImagePropertyPixelWidth] as? Int,
      let height = properties[kCGImagePropertyPixelHeight] as? Int,
      width > 0, height > 0, width <= 40_000_000 / height,
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
else { failClosed() }

do {
    let request = VNRecognizeTextRequest()
    // Revision 2 is the first Chinese-capable revision (macOS 11).
    request.revision = VNRecognizeTextRequestRevision2
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = false
    let supported = try VNRecognizeTextRequest.supportedRecognitionLanguages(for: .accurate, revision: request.revision)
    guard request.recognitionLanguages.allSatisfy({ supported.contains($0) }) else { failClosed() }
    // Both Pillow and this helper use the original, untransposed pixel raster.
    try VNImageRequestHandler(cgImage: image, orientation: .up, options: [:]).perform([request])
    guard let observations = request.results, !observations.isEmpty, observations.count <= 10_000
    else { failClosed() }
    var lines: [[String: Any]] = []
    for observation in observations {
        guard let candidate = observation.topCandidates(1).first,
              !candidate.string.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              candidate.string.count <= 4096,
              candidate.string.rangeOfCharacter(from: .controlCharacters) == nil
        else { failClosed() }
        let box = observation.boundingBox
        guard [box.minX, box.minY, box.maxX, box.maxY].allSatisfy({ $0.isFinite && $0 >= 0 && $0 <= 1 })
        else { failClosed() }
        let left = Int(floor(box.minX * Double(width)))
        let top = Int(floor((1 - box.maxY) * Double(height)))
        let right = Int(ceil(box.maxX * Double(width)))
        let bottom = Int(ceil((1 - box.minY) * Double(height)))
        guard right > left, bottom > top else { failClosed() }
        lines.append(["text": candidate.string, "left": left, "top": top,
                      "width": right - left, "height": bottom - top])
    }
    let output = try JSONSerialization.data(withJSONObject: ["width": width, "height": height, "lines": lines], options: [.sortedKeys])
    guard output.count <= 4 * 1024 * 1024 else { failClosed() }
    FileHandle.standardOutput.write(output)
    FileHandle.standardOutput.write(Data([10]))
} catch {
    failClosed()
}
