import Foundation
import ImageIO
import Vision

// Local-only semantic evidence helper. It emits JSON and never writes pixels.
// The Python wrapper treats this process as optional and falls back to bounded
// deterministic metrics when Vision is unavailable.

guard CommandLine.arguments.count == 2 else {
    fputs("usage: apple_vision_analyzer <image>\n", stderr)
    exit(2)
}

let imagePath = CommandLine.arguments[1]
let imageURL = URL(fileURLWithPath: imagePath)
guard let source = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fputs("unable to read image\n", stderr)
    exit(3)
}

let classification = VNClassifyImageRequest()
let faces = VNDetectFaceRectanglesRequest()
let animals = VNRecognizeAnimalsRequest()
let text = VNRecognizeTextRequest()
text.recognitionLevel = .fast
text.usesLanguageCorrection = false

do {
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try handler.perform([classification, faces, animals, text])
} catch {
    fputs("vision request failed: \(error)\n", stderr)
    exit(4)
}

func rectangle(_ value: CGRect) -> [String: Double] {
    ["x": Double(value.origin.x), "y": Double(value.origin.y),
     "width": Double(value.size.width), "height": Double(value.size.height)]
}

let labels: [[String: Any]] = (classification.results ?? []).prefix(20).map {
    ["identifier": $0.identifier, "confidence": Double($0.confidence)]
}
let faceBoxes: [[String: Any]] = (faces.results ?? []).map {
    ["confidence": Double($0.confidence), "bounding_box": rectangle($0.boundingBox)]
}
let animalLabels: [[String: Any]] = (animals.results ?? []).map { observation in
    let best = observation.labels.first
    return [
        "identifier": best?.identifier ?? "animal",
        "confidence": Double(best?.confidence ?? observation.confidence),
        "bounding_box": rectangle(observation.boundingBox),
    ]
}
let textObservations = (text.results ?? []).prefix(20).map { observation in
    ["confidence": Double(observation.confidence), "text": observation.topCandidates(1).first?.string ?? ""]
}

let result: [String: Any] = [
    "backend": "apple-vision",
    "backend_version": ProcessInfo.processInfo.operatingSystemVersionString,
    "path": imagePath,
    "classifications": labels,
    "faces": faceBoxes,
    "animals": animalLabels,
    "text": textObservations,
]

guard JSONSerialization.isValidJSONObject(result),
      let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]),
      let output = String(data: data, encoding: .utf8) else {
    fputs("unable to encode JSON\n", stderr)
    exit(5)
}
print(output)
