import Foundation

struct AuthUser: Codable, Identifiable, Hashable {
    let id: String
    let username: String
    let displayName: String
    let role: String
    let disabled: Bool

    enum CodingKeys: String, CodingKey {
        case id, username, role, disabled
        case displayName = "display_name"
    }
}

struct AuthConfigEnvelope: Codable {
    let configured: Bool
    let bootstrapRequired: Bool
    let registrationEnabled: Bool?
    let initialAdminUsername: String?
    let passwordMinChars: Int

    enum CodingKeys: String, CodingKey {
        case configured
        case bootstrapRequired = "bootstrap_required"
        case registrationEnabled = "registration_enabled"
        case initialAdminUsername = "initial_admin_username"
        case passwordMinChars = "password_min_chars"
    }
}

struct AuthSessionEnvelope: Codable {
    let token: String
    let expiresAt: Double
    let user: AuthUser

    enum CodingKeys: String, CodingKey {
        case token, user
        case expiresAt = "expires_at"
    }
}

struct AuthUserEnvelope: Codable {
    let user: AuthUser
    let expiresAt: Double?

    enum CodingKeys: String, CodingKey {
        case user
        case expiresAt = "expires_at"
    }
}

struct OKEnvelope: Codable {
    let ok: Bool
}

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
    let outputFormat: String?
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
    let artifactFilename: String?
    let artifactMediaType: String?
    let artifactSizeBytes: Int?
    let error: String
    let cancelRequested: Bool
    let attempt: Int
    let createdAt: Double
    let updatedAt: Double
    let startedAt: Double?
    let completedAt: Double?

    enum CodingKeys: String, CodingKey {
        case id, title, prompt, kind, status, source, progress, question, answers, error, attempt
        case outputFormat = "output_format"
        case deviceName = "device_name"
        case statusMessage = "status_message"
        case resultSummary = "result_summary"
        case reportId = "report_id"
        case reportFilename = "report_filename"
        case artifactFilename = "artifact_filename"
        case artifactMediaType = "artifact_media_type"
        case artifactSizeBytes = "artifact_size_bytes"
        case cancelRequested = "cancel_requested"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case startedAt = "started_at"
        case completedAt = "completed_at"
    }

    var isActive: Bool {
        ["queued", "running", "waiting_input"].contains(status)
    }

    var resolvedOutputFormat: String { outputFormat ?? "text" }

    var outputLabel: String {
        switch resolvedOutputFormat {
        case "image": "图片"
        case "illustrated": "图文报告"
        default: "文字报告"
        }
    }

    var outputSymbol: String {
        switch resolvedOutputFormat {
        case "image": "photo"
        case "illustrated": "doc.richtext"
        default: "doc.text"
        }
    }

    var hasArtifact: Bool {
        !(artifactFilename ?? "").isEmpty
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

struct ChatConversation: Codable, Identifiable, Hashable {
    let id: String
    let title: String
    let source: String
    let deviceName: String
    let preview: String?
    let messageCount: Int?
    let createdAt: Double
    let updatedAt: Double

    enum CodingKeys: String, CodingKey {
        case id, title, source, preview
        case deviceName = "device_name"
        case messageCount = "message_count"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}

struct ChatAttachment: Codable, Identifiable, Hashable {
    let id: String
    let conversationId: String
    let messageId: String
    let originalName: String
    let mediaType: String
    let kind: String
    let sizeBytes: Int
    let sha256: String
    let createdAt: Double

    enum CodingKeys: String, CodingKey {
        case id, kind, sha256
        case conversationId = "conversation_id"
        case messageId = "message_id"
        case originalName = "original_name"
        case mediaType = "media_type"
        case sizeBytes = "size_bytes"
        case createdAt = "created_at"
    }
}

struct ChatUploadAttachment: Identifiable, Hashable, Sendable {
    let id: UUID
    let filename: String
    let mediaType: String
    let data: Data
    let isImage: Bool

    init(
        id: UUID = UUID(),
        filename: String,
        mediaType: String,
        data: Data,
        isImage: Bool
    ) {
        self.id = id
        self.filename = filename
        self.mediaType = mediaType
        self.data = data
        self.isImage = isImage
    }
}

struct ChatMessage: Codable, Identifiable, Hashable {
    let id: String
    let conversationId: String
    let role: String
    let content: String
    let state: String
    let error: String
    let cancelRequested: Bool
    let createdAt: Double
    let updatedAt: Double
    let attachments: [ChatAttachment]?

    enum CodingKeys: String, CodingKey {
        case id, role, content, state, error, attachments
        case conversationId = "conversation_id"
        case cancelRequested = "cancel_requested"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    var isAssistant: Bool { role == "assistant" }
    var isActive: Bool { ["queued", "running"].contains(state) }
    var attachmentList: [ChatAttachment] { attachments ?? [] }
}

struct ChatConversationListEnvelope: Codable {
    let count: Int
    let conversations: [ChatConversation]
}

struct ChatConversationEnvelope: Codable {
    let conversation: ChatConversation
}

struct ChatMessagesEnvelope: Codable {
    let conversation: ChatConversation
    let count: Int
    let messages: [ChatMessage]
}

struct ChatTurnEnvelope: Codable {
    let userMessage: ChatMessage
    let assistantMessage: ChatMessage

    enum CodingKeys: String, CodingKey {
        case userMessage = "user_message"
        case assistantMessage = "assistant_message"
    }
}

struct ChatMessageEnvelope: Codable {
    let message: ChatMessage
}

struct DeleteChatEnvelope: Codable {
    let ok: Bool
    let deleted: String
}
