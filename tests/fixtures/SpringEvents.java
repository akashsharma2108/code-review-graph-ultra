package com.example.events;

import org.springframework.context.ApplicationEventPublisher;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;
import org.springframework.stereotype.Service;

// Plain event class — no Spring annotation
class OrderPlacedEvent {
    private final Long orderId;
    public OrderPlacedEvent(Long orderId) { this.orderId = orderId; }
    public Long getOrderId() { return orderId; }
}

// Publisher service
@Service
class OrderService {
    private final ApplicationEventPublisher eventPublisher;

    public OrderService(ApplicationEventPublisher eventPublisher) {
        this.eventPublisher = eventPublisher;
    }

    public void placeOrder(Long orderId) {
        // business logic
        eventPublisher.publishEvent(new OrderPlacedEvent(orderId));
    }
}

// Listener — infers event type from parameter
@Component
class NotificationListener {
    @EventListener
    public void onOrderPlaced(OrderPlacedEvent event) {
        // send notification
    }
}

// Listener — explicit annotation arg
@Component
class AuditListener {
    @EventListener(OrderPlacedEvent.class)
    public void auditOrder(OrderPlacedEvent event) {
        // audit log
    }
}
