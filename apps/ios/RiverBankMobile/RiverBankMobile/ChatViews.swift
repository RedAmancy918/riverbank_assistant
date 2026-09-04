import SwiftUI
import UIKit
import PhotosUI
import UniformTypeIdentifiers

private let chatCyan = Color(red: 0.337, green: 0.847, blue: 1.0)
private let maxChatAttachmentBytes = 15 * 1024 * 1024
private let maxChatAttachmentTotalBytes = 30 * 1024 * 1024
private let maxChatAttachments = 4
private let chatImportTypes: [UTType] = {
    var types: [UTType] = [.pdf, .plainText]
    if let markdown = UTType(filenameExtension: "md") { types.append(markdown) }
    if let markdownLong = UTType(filenameExtension: "markdown") { types.append(markdownLong) }
    return types
}()

struct ChatView: View {
    @EnvironmentObject private var store: AppStore
    @State private var draft = ""
    @State private var showHistory = false
    @State private var localError = ""
    @State private var pendingAttachments: [ChatUploadAttachment] = []
    @State private var selectedPhoto: PhotosPickerItem?
    @State private var showFileImporter = false
    @State private var isSubmitting = false
    @FocusState private var composerFocused: Bool

    private var isResponding: Bool {
        store.chatMessages.contains { $0.isAssistant && $0.isActive }
    }

    private var activeTitle: String {
        guard let id = store.activeChatID else { return "RiverBank" }
        return store.chats.first(where: { $0.id == id })?.title ?? "RiverBank"
    }

    var body: some View {
        NavigationStack {
            ChatTranscript(
                messages: store.chatMessages,
                dismissKeyboard: { composerFocused = false }
            ) { content in
                Task { await retry(content) }
            }
            .navigationTitle(activeTitle)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { showHistory = true } label: {
                        Image(systemName: "line.3.horizontal")
                    }
                    .accessibilityLabel("对话记录")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { Task { await startNewChat() } } label: {
                        Image(systemName: "square.and.pencil")
                    }
                    .accessibilityLabel("新对话")
                }
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("收起") { composerFocused = false }
                }
            }
            .safeAreaInset(edge: .bottom, spacing: 0) {
                ChatComposer(
                    text: $draft,
                    attachments: $pendingAttachments,
                    selectedPhoto: $selectedPhoto,
                    focused: $composerFocused,
                    isResponding: isResponding,
                    isSubmitting: isSubmitting,
                    openFiles: { showFileImporter = true },
                    send: { Task { await send() } },
                    stop: { Task { await stop() } }
                )
            }
            .sheet(isPresented: $showHistory) {
                ChatHistoryView(isPresented: $showHistory)
                    .environmentObject(store)
            }
            .fileImporter(
                isPresented: $showFileImporter,
                allowedContentTypes: chatImportTypes,
                allowsMultipleSelection: true
            ) { result in
                Task { await importDocuments(result) }
            }
            .onChange(of: selectedPhoto) { _, item in
                guard let item else { return }
                Task { await importPhoto(item) }
            }
            .task {
                await store.refreshChats(showLoading: true)
                if store.activeChatID == nil, let first = store.chats.first {
                    await store.selectChat(first)
                } else {
                    await store.refreshChatMessages()
                }
                while !Task.isCancelled {
                    let delay = isResponding ? 0.55 : 2.5
                    try? await Task.sleep(for: .seconds(delay))
                    if isResponding { await store.refreshChatMessages() }
                }
            }
            .alert("Chat 操作失败", isPresented: .constant(!localError.isEmpty)) {
                Button("好") { localError = "" }
            } message: {
                Text(localError)
            }
        }
    }

    private func startNewChat() async {
        do {
            _ = try await store.newChat()
            draft = ""
            pendingAttachments = []
            composerFocused = true
        } catch {
            if !error.isRiverBankCancellation { localError = error.localizedDescription }
        }
    }

    private func send() async {
        let content = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard (!content.isEmpty || !pendingAttachments.isEmpty),
              !isResponding,
              !isSubmitting else { return }
        isSubmitting = true
        defer { isSubmitting = false }
        do {
            try await store.sendChat(content, attachments: pendingAttachments)
            draft = ""
            pendingAttachments = []
        } catch {
            if !error.isRiverBankCancellation { localError = error.localizedDescription }
        }
    }

    private func stop() async {
        do { try await store.stopChatResponse() }
        catch {
            if !error.isRiverBankCancellation { localError = error.localizedDescription }
        }
    }

    private func retry(_ content: String) async {
        guard !content.isEmpty, !isResponding else { return }
        do { try await store.sendChat(content) }
        catch {
            draft = content
            composerFocused = true
            if !error.isRiverBankCancellation { localError = error.localizedDescription }
        }
    }

    private func appendAttachment(_ attachment: ChatUploadAttachment) {
        guard pendingAttachments.count < maxChatAttachments else {
            localError = "单条消息最多上传 4 个附件"
            return
        }
        guard !attachment.data.isEmpty, attachment.data.count <= maxChatAttachmentBytes else {
            localError = "单个附件不能为空或超过 15 MB"
            return
        }
        guard pendingAttachments.reduce(0, { $0 + $1.data.count }) + attachment.data.count
                <= maxChatAttachmentTotalBytes else {
            localError = "单条消息附件总计不能超过 30 MB"
            return
        }
        guard !attachment.isImage || !pendingAttachments.contains(where: \.isImage) else {
            localError = "单条消息最多上传 1 张图片"
            return
        }
        pendingAttachments.append(attachment)
    }

    private func importPhoto(_ item: PhotosPickerItem) async {
        defer { selectedPhoto = nil }
        do {
            guard let data = try await item.loadTransferable(type: Data.self) else {
                throw ChatUploadError.unreadable
            }
            appendAttachment(try normalizedPhotoUpload(data))
        } catch {
            if !error.isRiverBankCancellation { localError = error.localizedDescription }
        }
    }

    private func importDocuments(_ result: Result<[URL], Error>) async {
        do {
            let urls = try result.get()
            for url in urls {
                let attachment = try await Task.detached(priority: .userInitiated) {
                    try documentUpload(url)
                }.value
                appendAttachment(attachment)
            }
        } catch {
            if !error.isRiverBankCancellation { localError = error.localizedDescription }
        }
    }
}

private struct ChatTranscript: View {
    let messages: [ChatMessage]
    let dismissKeyboard: () -> Void
    let retry: (String) -> Void

    var body: some View {
        GeometryReader { viewport in
            ScrollViewReader { proxy in
                ScrollView {
                    if messages.isEmpty {
                        ChatWelcomeView()
                            .frame(maxWidth: .infinity)
                            .frame(minHeight: viewport.size.height, alignment: .center)
                    } else {
                        LazyVStack(spacing: 22) {
                            ForEach(Array(messages.enumerated()), id: \.element.id) { index, message in
                                ChatMessageView(
                                    message: message,
                                    retryContent: precedingUserContent(at: index),
                                    retry: retry
                                )
                                    .id(message.id)
                            }
                        }
                        .padding(.horizontal, 16)
                        .padding(.top, 18)
                        .padding(.bottom, 18)
                    }
                }
                .scrollDismissesKeyboard(.interactively)
                .simultaneousGesture(
                    TapGesture().onEnded { dismissKeyboard() }
                )
                .defaultScrollAnchor(.bottom)
                .onChange(of: messages) { _, updated in
                    guard let last = updated.last else { return }
                    withAnimation(.easeOut(duration: 0.22)) {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }
        }
        .background(Color(.systemBackground))
    }

    private func precedingUserContent(at index: Int) -> String? {
        guard index > 0 else { return nil }
        return messages[..<index].last(where: { $0.role == "user" })?.content
    }
}

private struct ChatWelcomeView: View {
    var body: some View {
        VStack(spacing: 18) {
            Image("RiverBankMark")
                .resizable()
                .scaledToFit()
                .frame(width: 62, height: 62)
                .padding(12)
                .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 22))
            Text("有什么可以帮你？")
                .font(.title2.weight(.semibold))
            Text("可以直接提问、讨论想法；复杂调研和文件整理可交给后台任务。")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 34)
        }
    }
}

private struct ChatMessageView: View {
    let message: ChatMessage
    let retryContent: String?
    let retry: (String) -> Void

    private var markdown: AttributedString {
        (try? AttributedString(markdown: message.content)) ?? AttributedString(message.content)
    }

    var body: some View {
        if message.role == "user" {
            HStack {
                Spacer(minLength: 46)
                VStack(alignment: .trailing, spacing: 8) {
                    ChatAttachmentList(attachments: message.attachmentList)
                    if !message.content.isEmpty {
                        Text(message.content)
                            .textSelection(.enabled)
                            .padding(.horizontal, 15)
                            .padding(.vertical, 11)
                            .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 20))
                    }
                }
            }
        } else {
            HStack(alignment: .top, spacing: 11) {
                Image("RiverBankMark")
                    .resizable()
                    .scaledToFit()
                    .frame(width: 27, height: 27)
                    .padding(.top, 1)
                VStack(alignment: .leading, spacing: 9) {
                    if message.content.isEmpty && message.isActive {
                        ThinkingIndicator()
                    } else if !message.content.isEmpty {
                        Text(markdown)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if message.state == "failed" {
                        Label(message.error.isEmpty ? "回答失败" : message.error, systemImage: "exclamationmark.circle")
                            .font(.caption)
                            .foregroundStyle(.red)
                    } else if message.state == "cancelled" {
                        Text("已停止生成")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    ChatAttachmentList(attachments: message.attachmentList)
                    if ["failed", "cancelled"].contains(message.state), let retryContent {
                        Button("重试") { retry(retryContent) }
                            .font(.caption.weight(.medium))
                    }
                    if !message.content.isEmpty && !message.isActive {
                        Button {
                            UIPasteboard.general.string = message.content
                        } label: {
                            Image(systemName: "doc.on.doc")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("复制回答")
                    }
                }
                Spacer(minLength: 12)
            }
        }
    }
}

private struct ChatAttachmentList: View {
    let attachments: [ChatAttachment]

    var body: some View {
        ForEach(attachments) { attachment in
            if attachment.kind == "image" {
                AuthenticatedChatImage(attachment: attachment)
            } else {
                Label(attachment.originalName, systemImage: "doc.text")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 7)
                    .background(Color(.secondarySystemBackground), in: Capsule())
            }
        }
    }
}

private struct AuthenticatedChatImage: View {
    @EnvironmentObject private var store: AppStore
    let attachment: ChatAttachment
    @State private var image: UIImage?
    @State private var failed = false

    var body: some View {
        Group {
            if let image {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFit()
            } else if failed {
                Label("图片加载失败", systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 90)
            } else {
                ProgressView("正在读取图片…")
                    .frame(maxWidth: .infinity, minHeight: 120)
            }
        }
        .frame(maxWidth: 320)
        .background(Color(.secondarySystemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
        .task(id: attachment.id) {
            do {
                let data = try await store.chatAttachmentData(attachment)
                image = UIImage(data: data)
                failed = image == nil
            } catch {
                if !error.isRiverBankCancellation { failed = true }
            }
        }
    }
}

private struct ThinkingIndicator: View {
    @State private var active = false

    var body: some View {
        HStack(spacing: 5) {
            ForEach(0..<3, id: \.self) { index in
                Circle()
                    .fill(chatCyan)
                    .frame(width: 6, height: 6)
                    .opacity(active ? 1 : 0.28)
                    .animation(
                        .easeInOut(duration: 0.65).repeatForever().delay(Double(index) * 0.14),
                        value: active
                    )
            }
        }
        .frame(height: 27)
        .onAppear { active = true }
    }
}

private struct ChatComposer: View {
    @Binding var text: String
    @Binding var attachments: [ChatUploadAttachment]
    @Binding var selectedPhoto: PhotosPickerItem?
    var focused: FocusState<Bool>.Binding
    let isResponding: Bool
    let isSubmitting: Bool
    let openFiles: () -> Void
    let send: () -> Void
    let stop: () -> Void

    private var canSend: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attachments.isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            if !attachments.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(attachments) { attachment in
                            HStack(spacing: 6) {
                                if attachment.isImage, let image = UIImage(data: attachment.data) {
                                    Image(uiImage: image)
                                        .resizable()
                                        .scaledToFill()
                                        .frame(width: 34, height: 34)
                                        .clipShape(RoundedRectangle(cornerRadius: 8))
                                } else {
                                    Image(systemName: attachment.mediaType == "application/pdf" ? "doc.richtext" : "doc.text")
                                        .foregroundStyle(chatCyan)
                                }
                                Text(attachment.filename)
                                    .font(.caption)
                                    .lineLimit(1)
                                    .frame(maxWidth: 150)
                                Button {
                                    attachments.removeAll { $0.id == attachment.id }
                                } label: {
                                    Image(systemName: "xmark.circle.fill")
                                        .foregroundStyle(.secondary)
                                }
                                .buttonStyle(.plain)
                                .accessibilityLabel("移除 \(attachment.filename)")
                            }
                            .padding(6)
                            .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 12))
                        }
                    }
                    .padding(.horizontal, 2)
                }
            }

            HStack(alignment: .bottom, spacing: 9) {
                PhotosPicker(selection: $selectedPhoto, matching: .images) {
                    Image(systemName: "photo")
                        .frame(width: 30, height: 34)
                }
                .disabled(isResponding || isSubmitting || attachments.count >= maxChatAttachments)
                .accessibilityLabel("选择图片")

                Button(action: openFiles) {
                    Image(systemName: "paperclip")
                        .frame(width: 30, height: 34)
                }
                .disabled(isResponding || isSubmitting || attachments.count >= maxChatAttachments)
                .accessibilityLabel("选择 PDF 或文本文件")

                TextField("询问任何问题", text: $text, axis: .vertical)
                    .lineLimit(1...6)
                    .focused(focused)
                    .padding(.leading, 2)
                    .padding(.vertical, 8)
                    .submitLabel(.send)
                    .disabled(isSubmitting)
                    .onSubmit {
                        if canSend && !isSubmitting { send() }
                    }

                Button(action: isResponding ? stop : send) {
                    Group {
                        if isSubmitting {
                            ProgressView().tint(.black)
                        } else {
                            Image(systemName: isResponding ? "stop.fill" : "arrow.up")
                                .font(.system(size: 15, weight: .bold))
                        }
                    }
                    .foregroundStyle(.black)
                    .frame(width: 34, height: 34)
                    .background(
                        isResponding || canSend ? chatCyan : Color.secondary.opacity(0.25),
                        in: Circle()
                    )
                }
                .disabled(isSubmitting || (!isResponding && !canSend))
                .accessibilityLabel(isResponding ? "停止生成" : "发送")
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 25))
        .overlay {
            RoundedRectangle(cornerRadius: 25)
                .stroke(Color.primary.opacity(0.09), lineWidth: 0.7)
        }
        .padding(.horizontal, 12)
        .padding(.top, 7)
        .padding(.bottom, 6)
        .background(.bar)
    }
}

private enum ChatUploadError: LocalizedError {
    case unreadable
    case unsupported
    case tooLarge

    var errorDescription: String? {
        switch self {
        case .unreadable: "无法读取所选文件"
        case .unsupported: "第一版仅支持图片、PDF、Markdown 和 TXT"
        case .tooLarge: "单个附件不能超过 15 MB"
        }
    }
}

private func normalizedPhotoUpload(_ source: Data) throws -> ChatUploadAttachment {
    guard let image = UIImage(data: source), image.size.width > 0, image.size.height > 0 else {
        throw ChatUploadError.unreadable
    }
    let maximumDimension: CGFloat = 4096
    let scale = min(1, maximumDimension / max(image.size.width, image.size.height))
    let target = CGSize(
        width: max(1, floor(image.size.width * scale)),
        height: max(1, floor(image.size.height * scale))
    )
    let format = UIGraphicsImageRendererFormat()
    format.scale = 1
    format.opaque = true
    let normalized = UIGraphicsImageRenderer(size: target, format: format).image { _ in
        image.draw(in: CGRect(origin: .zero, size: target))
    }
    guard let data = normalized.jpegData(compressionQuality: 0.88),
          !data.isEmpty else {
        throw ChatUploadError.unreadable
    }
    guard data.count <= maxChatAttachmentBytes else { throw ChatUploadError.tooLarge }
    return ChatUploadAttachment(
        filename: "RiverBank-Photo-\(UUID().uuidString.prefix(8)).jpg",
        mediaType: "image/jpeg",
        data: data,
        isImage: true
    )
}

private func documentUpload(_ url: URL) throws -> ChatUploadAttachment {
    let accessed = url.startAccessingSecurityScopedResource()
    defer { if accessed { url.stopAccessingSecurityScopedResource() } }
    let extensionName = url.pathExtension.lowercased()
    let mediaType: String
    switch extensionName {
    case "pdf": mediaType = "application/pdf"
    case "md", "markdown": mediaType = "text/markdown"
    case "txt": mediaType = "text/plain"
    default: throw ChatUploadError.unsupported
    }
    let values = try url.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey])
    guard values.isRegularFile == true else { throw ChatUploadError.unreadable }
    if let size = values.fileSize, size > maxChatAttachmentBytes {
        throw ChatUploadError.tooLarge
    }
    let data = try Data(contentsOf: url, options: .mappedIfSafe)
    guard !data.isEmpty else { throw ChatUploadError.unreadable }
    guard data.count <= maxChatAttachmentBytes else { throw ChatUploadError.tooLarge }
    return ChatUploadAttachment(
        filename: url.lastPathComponent,
        mediaType: mediaType,
        data: data,
        isImage: false
    )
}

private struct ChatHistoryView: View {
    @EnvironmentObject private var store: AppStore
    @Binding var isPresented: Bool
    @State private var localError = ""

    var body: some View {
        NavigationStack {
            List {
                Button {
                    Task {
                        do {
                            _ = try await store.newChat()
                            isPresented = false
                        } catch {
                            localError = error.localizedDescription
                        }
                    }
                } label: {
                    Label("新对话", systemImage: "square.and.pencil")
                        .font(.headline)
                }
                ForEach(store.chats) { conversation in
                    Button {
                        Task {
                            await store.selectChat(conversation)
                            isPresented = false
                        }
                    } label: {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(conversation.title)
                                .font(.body.weight(.medium))
                                .lineLimit(1)
                            if let preview = conversation.preview, !preview.isEmpty {
                                Text(preview)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                        }
                    }
                    .swipeActions {
                        Button(role: .destructive) {
                            Task {
                                do { try await store.deleteChat(conversation) }
                                catch { localError = error.localizedDescription }
                            }
                        } label: {
                            Label("删除", systemImage: "trash")
                        }
                    }
                }
            }
            .navigationTitle("对话")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") { isPresented = false }
                }
            }
            .refreshable { await store.refreshChats() }
            .alert("操作失败", isPresented: .constant(!localError.isEmpty)) {
                Button("好") { localError = "" }
            } message: { Text(localError) }
        }
    }
}
