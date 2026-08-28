import Foundation

struct TaskAnswer: Codable, Hashable {
    let question: String
    let answer: String
    let answeredAt: Double

    enum CodingKeys: String, CodingKey {
        case question, answer
        case answeredAt = "answered_at"
    }
}

struct RemoteTask: Codable, Identifiable, Hashable {
    let id: String
    let title: String
    let prompt: String
    let kind: String
    let status: String
    let source: String
    let deviceName: String
    let progress: Double
    let statusMessage: String
    let question: String
    let answers: [TaskAnswer]
    let resultSummary: String
    let reportId: String
    let reportFilename: String
    let error: String
    let cancelRequested: Bool
    let attempt: Int
    let createdAt: Double
    let updatedAt: Double
    let startedAt: Double?
    let completedAt: Double?

    enum CodingKeys: String, CodingKey {
        case id, title, prompt, kind, status, source, progress, question, answers, error, attempt
        case deviceName = "device_name"
        case statusMessage = "status_message"
        case resultSummary = "result_summary"
        case reportId = "report_id"
        case reportFilename = "report_filename"
        case cancelRequested = "cancel_requested"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case startedAt = "started_at"
        case completedAt = "completed_at"
    }

    var isActive: Bool {
        ["queued", "running", "waiting_input"].contains(status)
    }

    var statusLabel: String {
        switch status {
        case "queued": "排队中"
        case "running": "执行中"
        case "waiting_input": "待补充"
        case "completed": "已完成"
        case "failed": "失败"
        case "cancelled": "已取消"
        default: status
        }
    }
}

struct TaskEnvelope: Codable {
    let task: RemoteTask
}

struct CreateTaskEnvelope: Codable {
    let created: Bool
    let task: RemoteTask
}

struct TaskListEnvelope: Codable {
    let count: Int
    let tasks: [RemoteTask]
    let counts: [String: Int]
}

struct ReportRecord: Codable, Identifiable, Hashable {
    let id: String
    let title: String
    let summary: String
    let filename: String
    let relativePath: String
    let sizeBytes: Int
    let modifiedAt: String
    let format: String

    enum CodingKeys: String, CodingKey {
        case id, title, summary, filename, format
        case relativePath = "relative_path"
        case sizeBytes = "size_bytes"
        case modifiedAt = "modified_at"
    }
}

struct ReportListEnvelope: Codable {
    let count: Int
    let reports: [ReportRecord]
}

struct ReportContentEnvelope: Codable {
    let id: String
    let filename: String
    let sizeBytes: Int
    let modifiedAt: String
    let content: String

    enum CodingKeys: String, CodingKey {
        case id, filename, content
        case sizeBytes = "size_bytes"
        case modifiedAt = "modified_at"
    }
}

struct StatusEnvelope: Codable {
    let schema: String
    let startedAt: Double

    enum CodingKeys: String, CodingKey {
        case schema
        case startedAt = "started_at"
    }
}
