from enum import Enum

class State (Enum):
    INITIAL = "initial"
    MAIN_MENU = "main_menu"
    CHOSING_PRODUCT = "chosing_product"
    WAITING_ACTION = "waiting_action"
    FINALIZING_ORDER = "finalizing_order"
    ATTENDANT = "attendant"
    
    def __str__(self):
        return self.value
    
    @classmethod
    def get_all_values(cls):
        """Retorna uma lista com todos os valores dos estados"""
        return [state.value for state in cls]
    
    @classmethod
    def get_state_by_value(cls, value: str):
        """Retorna o estado correspondente ao valor ou None se não encontrar"""
        for state in cls:
            if state.value == value:
                return state
        return None