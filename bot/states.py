"""FSM state tanımları."""

from aiogram.fsm.state import State, StatesGroup


class UploadFlow(StatesGroup):
    """/baslat onay akışının adımları."""

    choosing_folder = State()
    choosing_channel = State()
    choosing_image_source = State()
    choosing_saved_image = State()
    waiting_photo = State()
    confirming_save_image = State()
    confirming_title = State()
    editing_title = State()
    confirming_description = State()
    editing_description = State()
    final_confirm = State()
    uploading = State()


class ChannelFlow(StatesGroup):
    """/kanallar → kanal ekleme."""

    waiting_name = State()
